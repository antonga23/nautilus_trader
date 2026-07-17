"""
Hermetic tests for the nodeops -> Postgres shipper.

No network DB is used.

"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import insert_odds, insert_sample, rebuild_nodeops_db

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
    assert json.loads(fake_writer.cursors["sqlite:samples"])["rowid"] == 2

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
    assert json.loads(fake_writer.cursors["sqlite:samples"])["rowid"] == 1


def test_cursor_resume_from_stored_high_water_mark(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    fake_writer.cursors["sqlite:samples"] = "1"  # rowid 1 already shipped
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 1
    assert fake_writer.tables["samples"][0]["node"] == "nodeB"


# --- 1b. cursor identity: rebuild vs prune ----------------------------------


def _samples_cursor(fake_writer) -> dict:
    return json.loads(fake_writer.cursors["sqlite:samples"])


def _execute(db_path: Path, sql: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(sql)
    conn.commit()
    conn.close()


def _spy_start_rowids(shipper) -> list[int]:
    starts: list[int] = []
    source = shipper._source
    original = source.read_new_rows

    def spying(table, columns, after_rowid, limit):
        if table == "samples":
            starts.append(after_rowid)
        return original(table, columns, after_rowid, limit)

    source.read_new_rows = spying
    return starts


def test_rebuild_same_max_rowid_detected_by_sha_and_reshipped(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 2

    # Fresh db with rowids restarted: rowid 2 exists again but holds different data,
    # so only the fingerprint can tell it apart from the shipped one.
    rebuild_nodeops_db(nodeops_db)
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")  # same natural key
    insert_sample(nodeops_db, "nodeC", "2026-07-16T00:02:00Z")  # new

    shipper._ship_sqlite()
    # Re-ship from 0: nodeA collides on (node, ts_utc), nodeC inserts. No dupes.
    assert len(fake_writer.tables["samples"]) == 3
    nodes = sorted(r["node"] for r in fake_writer.tables["samples"])
    assert nodes == ["nodeA", "nodeB", "nodeC"]
    cursor = _samples_cursor(fake_writer)
    assert cursor["rowid"] == 2
    assert isinstance(cursor["sha"], str)


def test_rebuild_with_fewer_rows_detected_by_max_rowid(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()

    rebuild_nodeops_db(nodeops_db)
    insert_sample(nodeops_db, "nodeC", "2026-07-16T00:02:00Z")

    shipper._ship_sqlite()  # stored rowid 2 > max rowid 1 -> rebuilt -> reset
    assert len(fake_writer.tables["samples"]) == 3
    assert _samples_cursor(fake_writer)["rowid"] == 1


def test_foreign_cursor_against_empty_db_resets_and_persists(tmp_path, nodeops_db, fake_writer):
    # New-host case: shipper_state carries another host's cursor, local db is empty.
    fake_writer.cursors["sqlite:samples"] = json.dumps({"rowid": 7, "sha": "0" * 64})
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()  # must not crash
    assert fake_writer.tables.get("samples", []) == []
    # The reset is persisted (a retreat, never an advance) so the next cycle does not
    # re-run detection against the dead cursor.
    assert _samples_cursor(fake_writer) == {"rowid": 0, "sha": None}


def test_prune_below_cursor_does_not_reset(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()

    _execute(nodeops_db, "DELETE FROM samples WHERE rowid = 1")  # retention prune
    insert_sample(nodeops_db, "nodeC", "2026-07-16T00:02:00Z")

    starts = _spy_start_rowids(shipper)
    shipper._ship_sqlite()
    assert starts[0] == 2  # resumed from the stored cursor, no reset
    assert len(fake_writer.tables["samples"]) == 3
    assert _samples_cursor(fake_writer)["rowid"] == 3


def test_prune_including_cursor_row_does_not_reset(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()

    _execute(nodeops_db, "DELETE FROM samples WHERE rowid <= 2")
    # AUTOINCREMENT keeps rowids monotonic: the new row lands at rowid 3, above the
    # stored cursor, so this is prune (row missing, max >= stored), not a rebuild.
    insert_sample(nodeops_db, "nodeC", "2026-07-16T00:02:00Z")

    starts = _spy_start_rowids(shipper)
    shipper._ship_sqlite()
    assert starts[0] == 2
    assert len(fake_writer.tables["samples"]) == 3
    assert _samples_cursor(fake_writer)["rowid"] == 3


def test_mutated_cursor_row_detected_by_sha(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()

    _execute(nodeops_db, "UPDATE samples SET node = 'other' WHERE rowid = 2")

    shipper._ship_sqlite()  # fingerprint mismatch -> reset -> re-ship
    # nodeA dedups; the mutated row has a new natural key so it inserts.
    assert len(fake_writer.tables["samples"]) == 3
    assert _samples_cursor(fake_writer)["rowid"] == 2


def test_legacy_bare_int_cursor_resumes_and_upgrades_on_advance(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    fake_writer.cursors["sqlite:samples"] = "1"  # pre-fingerprint format
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()
    assert [r["node"] for r in fake_writer.tables["samples"]] == ["nodeB"]
    cursor = _samples_cursor(fake_writer)
    assert cursor["rowid"] == 2
    assert isinstance(cursor["sha"], str)


def test_legacy_bare_int_cursor_kept_when_no_new_rows(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    fake_writer.cursors["sqlite:samples"] = "2"
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()  # nothing to ship; upgrade deferred to the next advance
    assert fake_writer.tables.get("samples", []) == []
    assert fake_writer.cursors["sqlite:samples"] == "2"


def test_pg_failure_during_rebuild_reship_preserves_cursor(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()
    cursor_before = fake_writer.cursors["sqlite:samples"]

    rebuild_nodeops_db(nodeops_db)
    insert_sample(nodeops_db, "nodeC", "2026-07-16T00:02:00Z")

    fake_writer.fail_ship_predicate = lambda n: True
    shipper._ship_sqlite()  # swallowed; the reset transaction rolled back
    assert fake_writer.cursors["sqlite:samples"] == cursor_before
    assert len(fake_writer.tables["samples"]) == 2

    fake_writer.fail_ship_predicate = None
    shipper._ship_sqlite()  # re-detects the rebuild and ships
    assert len(fake_writer.tables["samples"]) == 3
    assert _samples_cursor(fake_writer)["rowid"] == 1


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
    for table in (
        "arb_pnl_samples",
        "live_execution_samples",
        "arb_approvals",
        "arb_approval_stats",
        "arb_pairs",
        "trade_legs",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in fake_writer.schema_sql


# --- 7. flat trade tables from status.json ---------------------------------

SNAPSHOT_TS = "2026-07-16T12:00:00Z"


def _full_status_payload(ts: str = SNAPSHOT_TS, odds_a: str = "2.10") -> dict:
    return {
        "updatedAt": ts,
        "runtimeProbe": {
            "arbPositionPnl": {
                "pairs_tracked": 2,
                "pairs_open": 1,
                "pairs_settled": 1,
                "open_exposure": "10.5",
                "open_guaranteed_pnl": "0.25",
                "realized_pnl": "1.75",
                "settlements_received": 3,
                "settlements_unmatched": 0,
                "pairs": [
                    {
                        "pair_id": "O-1|O-2",
                        "settled": False,
                        "void": False,
                        "fully_hedged": True,
                        "cross_currency": False,
                        "base_currency": "USDT",
                        "winning_outcome": None,
                        "exposure": "10.5",
                        "guaranteed_pnl": "0.25",
                        "best_case_pnl": "0.30",
                        "realized_pnl": None,
                        "legs": [
                            {
                                "client_order_id": "O-1",
                                "venue": "SXBET",
                                "outcome": "HOME",
                                "side": "OrderSide.BUY",
                                "currency": "USDT",
                                "stake": "5",
                                "exposure": "5",
                                "fills": [{"px": "2.0", "qty": "5"}],
                                "settlement_result": "WON",
                            },
                            {
                                "client_order_id": "O-2",
                                "outcome": "AWAY",
                                "side": "OrderSide.BUY",
                                "currency": "USDT",
                                "stake": "5.5",
                                "exposure": "5.5",
                                "fills": 2,
                            },
                        ],
                    },
                ],
            },
            "strategyStats": {
                "live_execution": {
                    "kill_switch_active": False,
                    "halt_reason": None,
                    "realized_loss": "0",
                    "notional_used": "12.5",
                    "max_daily_notional": "100",
                    "max_daily_loss": "25",
                    "attempts": 4,
                    "blocks": 1,
                    "submissions": 3,
                    "block_reasons": {"stale_quote": 1},
                    "submissions_by_venue": {"SXBET": 3},
                },
            },
            "executionApprovals": {
                "mode": "manual",
                "ttl_secs": 300.0,
                "max_pending": 10,
                "staged": 4,
                "approved_executed": 1,
                "approved_blocked": 0,
                "rejected": 1,
                "expired": 1,
                "evicted": 0,
                "commands_processed": 3,
                "commands_invalid": 0,
                "pending": [
                    {
                        "approval_id": "APR-1",
                        "canonical_pair_id": "CP-1",
                        "created_at": "2026-07-16T11:59:00Z",
                        "expires_at": "2026-07-16T12:04:00Z",
                        "match_type": "EXACT",
                        "venue_a": "SXBET",
                        "venue_b": "CLOUDBET",
                        "instrument_id_a": "I-A.SXBET",
                        "instrument_id_b": "I-B.CLOUDBET",
                        "market_a": "Moneyline",
                        "market_b": "Moneyline",
                        "outcome_a": "HOME",
                        "outcome_b": "AWAY",
                        "odds_a": odds_a,
                        "odds_b": "2.05",
                        "stake_a": "5",
                        "stake_b": "5.1",
                        "fee_adjusted_profit_margin": "0.012",
                        "raw_profit_margin": "0.020",
                        "expected_profit": "0.12",
                    },
                ],
                "recent_decisions": [{"approval_id": "APR-0", "action": "approve"}],
            },
        },
    }


def _write_status(nodes_root: Path, node: str, payload: dict) -> None:
    node_dir = nodes_root / node
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "status.json").write_text(json.dumps(payload))


def test_flat_trades_full_payload_exact_values(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    _write_status(nodes_root, "nodeA", _full_status_payload())
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    shipper._ship_status_and_logs()

    assert fake_writer.tables["arb_pnl_samples"] == [
        {
            "node": "nodeA",
            "snapshot_ts": SNAPSHOT_TS,
            "pairs_tracked": 2,
            "pairs_open": 1,
            "pairs_settled": 1,
            "open_exposure": "10.5",
            "open_guaranteed_pnl": "0.25",
            "realized_pnl": "1.75",
            "settlements_received": 3,
            "settlements_unmatched": 0,
        },
    ]

    assert fake_writer.tables["live_execution_samples"] == [
        {
            "node": "nodeA",
            "snapshot_ts": SNAPSHOT_TS,
            "kill_switch_active": False,
            "halt_reason": None,
            "realized_loss": "0",
            "notional_used": "12.5",
            "max_daily_notional": "100",
            "max_daily_loss": "25",
            "attempts": 4,
            "blocks": 1,
            "submissions": 3,
            "block_reasons": {"stale_quote": 1},
            "submissions_by_venue": {"SXBET": 3},
        },
    ]

    (approval,) = fake_writer.tables["arb_approvals"]
    assert approval["approval_id"] == "APR-1"
    assert approval["canonical_pair_id"] == "CP-1"
    assert approval["odds_a"] == "2.10"
    assert approval["expected_profit"] == "0.12"
    assert approval["last_seen"] == SNAPSHOT_TS

    (stats,) = fake_writer.tables["arb_approval_stats"]
    assert stats["mode"] == "manual"
    assert stats["staged"] == 4
    assert stats["pending_count"] == 1
    assert stats["recent_decisions"] == [{"approval_id": "APR-0", "action": "approve"}]

    (pair,) = fake_writer.tables["arb_pairs"]
    assert pair["pair_id"] == "O-1|O-2"
    assert pair["fully_hedged"] is True
    assert pair["guaranteed_pnl"] == "0.25"
    assert pair["last_seen"] == SNAPSHOT_TS

    legs = {leg["client_order_id"]: leg for leg in fake_writer.tables["trade_legs"]}
    assert set(legs) == {"O-1", "O-2"}
    assert legs["O-1"]["venue"] == "SXBET"
    assert legs["O-1"]["fill_count"] == 1  # list fills -> len
    assert legs["O-1"]["fills"] == [{"px": "2.0", "qty": "5"}]
    assert legs["O-1"]["settlement_result"] == "WON"
    assert legs["O-2"]["venue"] is None
    assert legs["O-2"]["fill_count"] == 2  # int fills (current node format) kept as-is
    assert legs["O-2"]["fills"] is None
    assert legs["O-2"]["settlement_result"] is None


def test_flat_trades_pairs_absent_tolerated_silently(tmp_path, nodeops_db, fake_writer, caplog):
    payload = _full_status_payload()
    del payload["runtimeProbe"]["arbPositionPnl"]["pairs"]
    nodes_root = tmp_path / "nodes"
    _write_status(nodes_root, "nodeA", payload)
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    with caplog.at_level(logging.WARNING, logger="shipper"):
        shipper._ship_status_and_logs()

    assert "arb_pairs" not in fake_writer.tables
    assert "trade_legs" not in fake_writer.tables
    assert len(fake_writer.tables["arb_pnl_samples"]) == 1
    assert len(fake_writer.tables["live_execution_samples"]) == 1
    assert len(fake_writer.tables["arb_approvals"]) == 1
    assert not caplog.records  # expected absence is not warned about


def test_flat_trades_malformed_block_skipped_with_warning(
    tmp_path,
    nodeops_db,
    fake_writer,
    caplog,
):
    payload = _full_status_payload()
    payload["runtimeProbe"]["strategyStats"]["live_execution"] = "broken"
    nodes_root = tmp_path / "nodes"
    _write_status(nodes_root, "nodeA", payload)
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    with caplog.at_level(logging.WARNING, logger="shipper"):
        shipper._ship_status_and_logs()

    assert "live_execution_samples" not in fake_writer.tables
    assert any("live_execution_samples" in rec.message for rec in caplog.records)
    # One bad block never blocks the other flat tables.
    assert len(fake_writer.tables["arb_pnl_samples"]) == 1
    assert len(fake_writer.tables["arb_approvals"]) == 1
    assert len(fake_writer.tables["arb_pairs"]) == 1


def test_flat_trades_approval_upsert_updates_not_duplicates(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    _write_status(nodes_root, "nodeA", _full_status_payload())
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)
    shipper._ship_status_and_logs()

    later_ts = "2026-07-16T12:01:00Z"
    _write_status(nodes_root, "nodeA", _full_status_payload(ts=later_ts, odds_a="2.20"))
    shipper._ship_status_and_logs()

    (approval,) = fake_writer.tables["arb_approvals"]  # updated in place, no dupe
    assert approval["odds_a"] == "2.20"
    assert approval["last_seen"] == later_ts
    assert approval["created_at"] == "2026-07-16T11:59:00Z"


def test_flat_trades_unparsable_updated_at_skips_flat_rows(tmp_path, nodeops_db, fake_writer):
    payload = _full_status_payload(ts="not-a-timestamp")
    nodes_root = tmp_path / "nodes"
    _write_status(nodes_root, "nodeA", payload)
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    shipper._ship_status_and_logs()

    assert len(fake_writer.status) == 1  # raw snapshot still stored
    for table in (
        "arb_pnl_samples",
        "live_execution_samples",
        "arb_approvals",
        "arb_approval_stats",
        "arb_pairs",
        "trade_legs",
    ):
        assert table not in fake_writer.tables
