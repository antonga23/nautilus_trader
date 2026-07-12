# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Pure-stdlib unit tests for the nodeops dashboard server.

Loads ``tools/nodeops/server.py`` as a module (its ``__main__`` guard means importing
does not start the HTTP server) and exercises the SQLite store, history metric
filtering, HTTP Basic auth, read-only gating, the name/manifest validators, and
manifest secret-stripping. Docker, subprocess, and the filesystem are mocked with
``tmp_path``/``monkeypatch`` so no live docker or network is required.

"""

from __future__ import annotations

import base64
import http.client
import io
import json
import sqlite3
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


SCRIPT_PATH = Path("tools/nodeops/server.py")


def load_server() -> ModuleType:
    """
    Import ``server.py`` by path without triggering the ``__main__`` entry point.
    """
    spec = importlib.util.spec_from_file_location("nodeops_server", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server = load_server()


def _config(tmp_path: Path, **overrides: str) -> Any:
    env = {
        "NODEOPS_DB": str(tmp_path / "nodeops.db"),
        "NODEOPS_NODES_ROOT": str(tmp_path / "nodes"),
        "NODEOPS_REPO_DIR": str(tmp_path / "repo"),
        "NODEOPS_READONLY": "0",
    }
    env.update(overrides)
    return server.Config(env)


def _sample_row(node: str, ts_utc: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(server.SAMPLE_COLUMNS, 0)
    row["node"] = node
    row["ts_utc"] = ts_utc
    row["container_state"] = "running"
    row["image"] = "ghcr.io/x/node:abc"
    row.update(overrides)
    return row


# -- store roundtrip ------------------------------------------------------------


def test_store_schema_and_sample_roundtrip(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")

    tables = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert {"samples", "odds_samples"}.issubset(tables)
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    store.insert_sample(_sample_row("node-a", "2026-07-01T00:00:00Z", graph_edges=11))
    latest = store.latest_sample("node-a")
    assert latest is not None
    assert latest["graph_edges"] == 11
    assert latest["container_state"] == "running"
    store.close()


def test_latest_samples_returns_most_recent_per_node(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    store.insert_sample(_sample_row("node-a", "2026-07-01T00:00:00Z", quoted_edges=1))
    store.insert_sample(_sample_row("node-a", "2026-07-01T00:01:00Z", quoted_edges=2))
    store.insert_sample(_sample_row("node-b", "2026-07-01T00:00:30Z", quoted_edges=9))

    latest = store.latest_samples()
    assert set(latest) == {"node-a", "node-b"}
    assert latest["node-a"]["quoted_edges"] == 2
    assert latest["node-b"]["quoted_edges"] == 9
    store.close()


def test_odds_sample_roundtrip(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    store.insert_odds_sample(
        "2026-07-01T00:00:00Z",
        "node-a",
        "topPositiveCandidates",
        [{"margin": "0.03"}],
    )
    rows = store._conn.execute("SELECT node, kind, payload_json FROM odds_samples").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "topPositiveCandidates"
    assert "margin" in rows[0]["payload_json"]
    store.close()


def test_prune_removes_only_expired_rows(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.insert_sample(_sample_row("node-a", fresh))
    store.insert_sample(_sample_row("node-a", old))

    removed = store.prune(30)
    assert removed == 1
    remaining = store._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert remaining == 1
    store.close()


# -- history metric filtering ---------------------------------------------------


def test_history_filters_unknown_metrics(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.insert_sample(_sample_row("node-a", now, graph_edges=5, quoted_edges=3))

    history = store.history("node-a", 24.0, ["graph_edges", "not_a_column", "cpu_pct"])
    assert history["metrics"] == ["graph_edges", "cpu_pct"]
    assert "not_a_column" not in history["metrics"]
    assert history["points"][0]["graph_edges"] == 5
    assert "ts_utc" in history["points"][0]
    store.close()


def test_history_defaults_when_no_valid_metrics(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.insert_sample(_sample_row("node-a", now))

    history = store.history("node-a", 24.0, ["bogus"])
    assert history["metrics"] == ["graph_edges", "quoted_edges", "cross_venue_candidate_count"]
    store.close()


def test_history_respects_time_window(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    recent = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (datetime.now(UTC) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.insert_sample(_sample_row("node-a", stale, graph_edges=1))
    store.insert_sample(_sample_row("node-a", recent, graph_edges=2))

    history = store.history("node-a", 1.0, ["graph_edges"])
    assert [point["graph_edges"] for point in history["points"]] == [2]
    store.close()


# -- name / image / manifest validators ----------------------------------------


def test_valid_name_allowlist() -> None:
    assert server.valid_name("betting-arbitrage-node-multivenue")
    assert server.valid_name("node_1.2")
    assert not server.valid_name("../x")
    assert not server.valid_name("a b")
    assert not server.valid_name("a;b")
    assert not server.valid_name("")
    assert not server.valid_name("a/b")
    # Must start alphanumeric: no CLI-flag injection and no path traversal via a
    # dot-only segment (these all matched the previous ^[A-Za-z0-9._-]+$ pattern).
    assert not server.valid_name("-f")
    assert not server.valid_name("--all")
    assert not server.valid_name(".")
    assert not server.valid_name("..")
    assert not server.valid_name("...")


def test_safe_manifest_path_rejects_traversal_and_wrong_dir() -> None:
    good = "deploy/strategy_nodes/betting_arbitrage/cloudbet-single-venue.json"
    assert server.safe_manifest_path(good) == good
    assert server.safe_manifest_path("deploy/strategy_nodes/../../etc/passwd.json") is None
    assert server.safe_manifest_path("/abs/deploy/strategy_nodes/x.json") is None
    assert server.safe_manifest_path("deploy/strategy_nodes/x.yaml") is None
    assert server.safe_manifest_path("other/dir/x.json") is None
    assert server.safe_manifest_path("") is None


def test_valid_image_reference() -> None:
    assert server.valid_image("ghcr.io/org/node:1.2.3")
    assert server.valid_image("registry/name@sha256:deadbeef")
    assert not server.valid_image("bad image")
    assert not server.valid_image("name;rm -rf")
    assert not server.valid_image("")
    assert not server.valid_image("-v")
    assert not server.valid_image("--foo")


# -- secret stripping -----------------------------------------------------------


def test_strip_secrets_removes_credential_keys() -> None:
    payload = {
        "node_id": "n1",
        "CLOUDBET_API_KEY": "leak",
        "nested": {
            "PRIVATE_KEY": "leak",
            "safe": 1,
            "venues": [{"password": "leak", "venue": "CLOUDBET"}],
        },
        "auth_token": "leak",
        "some_secret": "leak",
    }
    cleaned = server.strip_secrets(payload)
    assert cleaned == {
        "node_id": "n1",
        "nested": {"safe": 1, "venues": [{"venue": "CLOUDBET"}]},
    }


# -- runtime-probe flattening ---------------------------------------------------


def test_build_sample_row_reads_probe_paths() -> None:
    now = datetime(2026, 7, 1, 0, 5, 0, tzinfo=UTC)
    status = {
        "runtimeProbe": {
            "subscribedInstruments": 42,
            "graphNodes": 30,
            "graphEdges": 18,
            "quotedEdges": 7,
            "semanticMatchInstruments": 5,
            "venueCoverage": {"crossVenueCandidateCount": 3},
            # ragBands is dead; RAG is now derived from marginBands + executionSafe
            # + executable_candidates. positive=6, caps=[6,4(safe),6(exec)]→green=4
            # (quoted!=0); amber=6-4=2; red=1 (the "0% to -1%" loss bucket).
            "candidateQuality": {
                "marginBands": {"positive": 6, "0% to -1%": 1},
                "executionSafeEdges": 4,
            },
            "strategyStats": {
                "raw_arbitrage_detections": 100,
                "opportunities_found": 12,
                "executable_candidates": 6,
                "opportunities_executed": 2,
            },
            "executionApprovals": {
                "mode": "manual",
                "pending": [{"approval_id": "a" * 12}, {"approval_id": "b" * 12}],
            },
        },
    }
    heartbeat = {"at": "2026-07-01T00:04:00Z"}
    inspect = {"state": "running", "image": "ghcr.io/x/node:abc"}
    stats = {"mem_mb": 512.0, "cpu_pct": 3.5}

    row = server.build_sample_row("node-a", status, heartbeat, inspect, stats, now)
    assert row["subscribed_instruments"] == 42
    assert row["graph_edges"] == 18
    assert row["quoted_edges"] == 7
    assert row["cross_venue_candidate_count"] == 3
    assert (row["rag_green"], row["rag_amber"], row["rag_red"]) == (4, 2, 1)
    assert row["raw_detections"] == 100
    assert row["valid_opportunities"] == 12
    assert row["executable_candidates"] == 6
    assert row["executed"] == 2
    assert row["pending_approvals"] == 2
    assert row["heartbeat_age_secs"] == 60.0
    assert row["container_state"] == "running"


def test_build_sample_row_defaults_missing_probe_to_zero() -> None:
    now = datetime.now(UTC)
    row = server.build_sample_row("node-a", None, None, None, {}, now)
    assert row["graph_edges"] == 0
    assert row["cross_venue_candidate_count"] == 0
    assert row["pending_approvals"] == 0
    assert row["heartbeat_age_secs"] is None
    assert row["mem_mb"] is None


# -- docker stats parsing (no live docker) --------------------------------------


def test_parse_mem_and_percent() -> None:
    assert server._parse_mem_mb("512MiB / 4GiB") == 512.0
    assert server._parse_mem_mb("1.5GiB / 4GiB") == 1536.0
    assert server._parse_percent("12.50%") == 12.5
    assert server._parse_mem_mb("garbage") is None


# -- uptime: StartedAt parsing + sampling ----------------------------------------


def test_parse_docker_timestamp_handles_nanoseconds_and_zero_time() -> None:
    stamp = server.parse_docker_timestamp("2026-07-04T17:46:05.123456789Z")
    assert stamp == datetime(2026, 7, 4, 17, 46, 5, 123456, tzinfo=UTC)
    assert server.parse_docker_timestamp("2026-07-04T17:46:05Z") == datetime(
        2026,
        7,
        4,
        17,
        46,
        5,
        tzinfo=UTC,
    )
    offset = server.parse_docker_timestamp("2026-07-04T18:46:05.123456789+01:00")
    assert offset == datetime(2026, 7, 4, 17, 46, 5, 123456, tzinfo=UTC)
    assert server.parse_docker_timestamp("0001-01-01T00:00:00Z") is None  # never started
    assert server.parse_docker_timestamp("garbage") is None
    assert server.parse_docker_timestamp("") is None
    assert server.parse_docker_timestamp(None) is None


def test_docker_inspect_reports_started_at(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        server,
        "_run_docker",
        lambda args, **k: "running\tghcr.io/x/node:abc\t2026-07-04T17:46:05.123456789Z\n",
    )
    inspect = server.docker_inspect("node-a")
    assert inspect == {
        "state": "running",
        "image": "ghcr.io/x/node:abc",
        "started_at": "2026-07-04T17:46:05Z",
    }


def test_build_sample_row_computes_uptime_when_running() -> None:
    now = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
    inspect = {"state": "running", "image": "i", "started_at": "2026-07-06T10:00:00Z"}
    row = server.build_sample_row("node-a", None, None, inspect, {}, now)
    assert row["started_at"] == "2026-07-06T10:00:00Z"
    assert row["uptime_secs"] == 7200.0


def test_build_sample_row_uptime_none_when_not_running() -> None:
    now = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
    exited = {"state": "exited", "image": "i", "started_at": "2026-07-06T10:00:00Z"}
    row = server.build_sample_row("node-a", None, None, exited, {}, now)
    assert row["started_at"] == "2026-07-06T10:00:00Z"
    assert row["uptime_secs"] is None
    missing = server.build_sample_row("node-a", None, None, None, {}, now)
    assert missing["started_at"] is None
    assert missing["uptime_secs"] is None


def test_store_uptime_roundtrip_and_history_metric(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.insert_sample(
        _sample_row("node-a", now, started_at="2026-07-06T10:00:00Z", uptime_secs=7200.0),
    )
    latest = store.latest_sample("node-a")
    assert latest is not None
    assert latest["started_at"] == "2026-07-06T10:00:00Z"
    assert latest["uptime_secs"] == 7200.0
    history = store.history("node-a", 24.0, ["uptime_secs"])
    assert history["metrics"] == ["uptime_secs"]
    assert history["points"][0]["uptime_secs"] == 7200.0
    store.close()


def test_store_migrates_old_schema_in_place(tmp_path: Path) -> None:
    """
    A DB created before started_at/uptime_secs existed upgrades via ALTER TABLE.
    """
    db_path = tmp_path / "nodeops.db"
    old_columns = [
        column
        for column in server.SAMPLE_COLUMNS
        if column not in {"started_at", "uptime_secs", "pending_approvals"}
    ]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{column} TEXT" for column in old_columns)
        + ")",
    )
    conn.commit()
    conn.close()

    store = server.Store(db_path)
    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(samples)")}
    assert {"started_at", "uptime_secs", "pending_approvals"}.issubset(columns)
    store.insert_sample(
        _sample_row(
            "node-a",
            "2026-07-06T12:00:00Z",
            started_at="2026-07-06T10:00:00Z",
            uptime_secs=7200.0,
        ),
    )
    latest = store.latest_sample("node-a")
    assert latest is not None
    assert latest["uptime_secs"] == 7200.0
    store.close()


# -- HTTP handler behaviour (auth, readonly) via a fake request -----------------


class _FakeHandler:
    """
    Minimal stand-in exposing the handler methods under test.
    """

    def __init__(self, state: Any, headers: dict[str, str], command: str = "GET") -> None:
        self.state = state
        self.headers = headers
        self.command = command
        self.sent: dict[str, Any] = {}

    _authorized = server.Handler._authorized
    _readonly_blocked = server.Handler._readonly_blocked

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def _basic_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def test_auth_accepts_correct_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="op", NODEOPS_PASSWORD="pw")
    state = server.NodeOpsState(config, object(), object())
    handler = _FakeHandler(state, {"Authorization": _basic_header("op", "pw")})
    assert handler._authorized() is True


def test_auth_rejects_wrong_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="op", NODEOPS_PASSWORD="pw")
    state = server.NodeOpsState(config, object(), object())
    assert (
        _FakeHandler(state, {"Authorization": _basic_header("op", "nope")})._authorized() is False
    )
    assert _FakeHandler(state, {})._authorized() is False


def test_auth_disabled_when_user_unset(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="")
    state = server.NodeOpsState(config, object(), object())
    assert config.auth_enabled is False
    assert _FakeHandler(state, {})._authorized() is True


def test_readonly_blocks_mutating_handler(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="1")
    state = server.NodeOpsState(config, object(), object())
    handler = _FakeHandler(state, {}, command="POST")
    assert handler._readonly_blocked() is True
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN


def test_readonly_allows_when_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="0")
    state = server.NodeOpsState(config, object(), object())
    handler = _FakeHandler(state, {}, command="POST")
    assert handler._readonly_blocked() is False


# -- sampler isolates per-node failures -----------------------------------------


def test_sampler_survives_one_bad_node(tmp_path: Path, monkeypatch: Any) -> None:
    nodes_root = tmp_path / "nodes"
    (nodes_root / "good").mkdir(parents=True)
    (nodes_root / "bad").mkdir()
    (nodes_root / "good" / "status.json").write_text(
        '{"runtimeProbe": {"graphEdges": 4}}',
        encoding="utf8",
    )
    (nodes_root / "good" / "heartbeat.json").write_text(
        '{"at": "2026-07-01T00:00:00Z"}',
        encoding="utf8",
    )
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)

    monkeypatch.setattr(server, "docker_inspect", lambda name: {"state": "running", "image": "i"})
    monkeypatch.setattr(server, "docker_stats", lambda name: {"mem_mb": 1.0, "cpu_pct": 1.0})

    real_build = server.build_sample_row

    def _explode_on_bad(node: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if node == "bad":
            raise RuntimeError("boom")
        return real_build(node, *args, **kwargs)

    monkeypatch.setattr(server, "build_sample_row", _explode_on_bad)

    sampler = server.Sampler(config, store, __import__("threading").Event())
    sampler.sample_once()  # must not raise despite the bad node

    latest = store.latest_samples()
    assert "good" in latest
    assert latest["good"]["graph_edges"] == 4
    assert "bad" not in latest
    store.close()


# -- manifest validation-safety gate (deploy must never arm execution) ----------


def _validation_manifest() -> dict[str, Any]:
    return {
        "validation_mode": True,
        "strategy": {"auto_execute": False, "value_execution_enabled": False},
        "venues": [{"venue": "CLOUDBET", "execution_enabled": False}],
    }


def _live_pilot_manifest() -> dict[str, Any]:
    return {
        "validation_mode": False,
        "strategy": {
            "auto_execute": True,
            "live_execution_armed": True,
            "allow_same_venue_live_execution": True,
        },
        "venues": [{"venue": "SXBET", "execution_enabled": True, "execution_dry_run": False}],
    }


def test_manifest_is_validation_safe_accepts_data_only() -> None:
    assert server.manifest_is_validation_safe(_validation_manifest()) is True


def test_manifest_is_validation_safe_rejects_live_pilot() -> None:
    assert server.manifest_is_validation_safe(_live_pilot_manifest()) is False


def test_manifest_is_validation_safe_rejects_each_arming_vector() -> None:
    # validation_mode missing/false
    assert not server.manifest_is_validation_safe({"strategy": {}, "venues": []})
    # auto_execute on despite validation_mode
    assert not server.manifest_is_validation_safe(
        {"validation_mode": True, "strategy": {"auto_execute": True}, "venues": []},
    )
    # a single venue with execution_enabled on
    assert not server.manifest_is_validation_safe(
        {
            "validation_mode": True,
            "strategy": {},
            "venues": [
                {"execution_enabled": False},
                {"execution_enabled": True},
            ],
        },
    )
    # dry-run execution readiness (execution_enabled true, validation_mode false)
    assert not server.manifest_is_validation_safe(
        {"validation_mode": False, "venues": [{"execution_enabled": True}]},
    )
    assert not server.manifest_is_validation_safe("not-a-dict")


class _DeployHandler:
    """
    Fake handler exercising ``_deploy_node`` end-to-end without spawning a job.
    """

    def __init__(self, state: Any, body: bytes) -> None:
        self.state = state
        self.command = "POST"
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent: dict[str, Any] = {}
        self.started: dict[str, Any] | None = None

    _deploy_node = server.Handler._deploy_node
    _read_body = server.Handler._read_body
    _readonly_blocked = server.Handler._readonly_blocked
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}

    def _start_job(self, kind: str, target: str, args: list[str]) -> None:
        self.started = {"kind": kind, "target": target, "args": args}
        self._send_json(server.HTTPStatus.ACCEPTED, {"job_id": "test"})


def _write_manifest(repo_dir: Path, filename: str, manifest: dict[str, Any]) -> str:
    rel = f"deploy/strategy_nodes/betting_arbitrage/{filename}"
    path = repo_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf8")
    return rel


def _deploy_body(manifest_rel: str) -> bytes:
    return json.dumps(
        {
            "container_name": "betting-arb-x",
            "image": "ghcr.io/x/node:tag",
            "manifest_path": manifest_rel,
        },
    ).encode("utf8")


def test_deploy_rejects_live_pilot_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo_dir = Path(config.repo_dir)
    rel = _write_manifest(repo_dir, "x-live-pilot.json", _live_pilot_manifest())
    handler = _DeployHandler(server.NodeOpsState(config, object(), object()), _deploy_body(rel))

    handler._deploy_node()

    assert handler.started is None  # never reached the job dispatch
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    assert "validation-safe" in handler.sent["payload"]["error"]


def test_deploy_accepts_validation_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    repo_dir = Path(config.repo_dir)
    rel = _write_manifest(repo_dir, "multi-venue-validation.json", _validation_manifest())
    handler = _DeployHandler(server.NodeOpsState(config, object(), object()), _deploy_body(rel))

    handler._deploy_node()

    assert handler.sent["status"] == server.HTTPStatus.ACCEPTED
    assert handler.started is not None
    assert rel in handler.started["args"]
    assert handler.started["args"][0] == "bash"


def test_deploy_rejects_missing_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rel = "deploy/strategy_nodes/betting_arbitrage/does-not-exist.json"
    handler = _DeployHandler(server.NodeOpsState(config, object(), object()), _deploy_body(rel))

    handler._deploy_node()

    assert handler.started is None
    assert handler.sent["status"] == server.HTTPStatus.BAD_REQUEST


# -- auth / bind hardening ------------------------------------------------------


def test_change_me_credentials_treated_as_unset(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="CHANGE_ME", NODEOPS_PASSWORD="CHANGE_ME")
    assert config.user is None
    assert config.password is None
    assert config.auth_enabled is False


def test_insecure_public_bind_detection(tmp_path: Path) -> None:
    assert server._is_loopback("127.0.0.1")
    assert server._is_loopback("::1")
    assert server._is_loopback("localhost")
    assert not server._is_loopback("0.0.0.0")  # noqa: S104 - asserting a non-loopback host
    assert not server._is_loopback("")  # binds all interfaces, not loopback

    public_no_auth = _config(tmp_path, NODEOPS_HOST="0.0.0.0", NODEOPS_USER="")  # noqa: S104 - test fixture host
    assert server._insecure_public_bind(public_no_auth) is True

    public_with_auth = _config(
        tmp_path,
        NODEOPS_HOST="0.0.0.0",  # noqa: S104 - test fixture host
        NODEOPS_USER="op",
        NODEOPS_PASSWORD="pw",
    )
    assert server._insecure_public_bind(public_with_auth) is False

    loopback_no_auth = _config(tmp_path, NODEOPS_HOST="127.0.0.1", NODEOPS_USER="")
    assert server._insecure_public_bind(loopback_no_auth) is False


def test_read_body_rejects_oversized(tmp_path: Path) -> None:
    config = _config(tmp_path)
    oversized = server.MAX_BODY_BYTES + 1
    handler = _DeployHandler(server.NodeOpsState(config, object(), object()), b"")
    handler.headers = {"Content-Length": str(oversized)}
    handler.rfile = io.BytesIO(b"{}")  # body never read because the cap trips first
    assert handler._read_body() == {}


# -- real HTTP smoke (exercises the actual Handler, not the fake) ----------------


def test_real_http_server_serves_nodes_and_index(tmp_path: Path) -> None:
    """
    Drive the real server over HTTP on an ephemeral port.

    The fake-handler tests inject ``state`` directly, so they never exercise the
    Handler.state resolution or the module import on the wire — this does both, and
    fails if ``self.state`` is unresolved (500) or the module cannot import.

    """
    nodes = tmp_path / "nodes"
    (nodes / "demo").mkdir(parents=True)
    (nodes / "demo" / "status.json").write_text(
        '{"runtimeProbe": {"graphEdges": 9, "quotedEdges": 2}}',
        encoding="utf8",
    )
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="1",
        NODEOPS_NODES_ROOT=str(nodes),
    )
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    srv = server.build_server(config, store, jobs)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/nodes")
        resp = conn.getresponse()
        body = resp.read().decode("utf8")
        assert resp.status == 200, body
        data = json.loads(body)
        assert data["readonly"] is True
        assert any(node["node"] == "demo" for node in data["nodes"])
        conn.request("GET", "/")
        index = conn.getresponse()
        index_body = index.read()
        assert index.status == 200
        assert index_body
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


# -- RAG derivation -------------------------------------------------------------


def test_derive_rag_quoting_node() -> None:
    probe = {
        "quotedEdges": 42,
        "candidateQuality": {
            "marginBands": {"positive": 28, "0% to -1%": 9, "-1% to -5%": 5, "< -5%": 3},
            "executionSafeEdges": 22,
        },
        "strategyStats": {"opportunities_found": 28, "executable_candidates": 22},
    }
    assert server._derive_rag(probe) == (22, 6, 17)


def test_derive_rag_zero_quote_forces_green_zero() -> None:
    probe = {
        "quotedEdges": 0,
        "candidateQuality": {"marginBands": {"positive": 4, "0% to -1%": 2}},
        "strategyStats": {"opportunities_found": 4, "executable_candidates": 0},
    }
    assert server._derive_rag(probe) == (0, 4, 2)


def test_derive_rag_no_margin_bands() -> None:
    assert server._derive_rag({}) == (0, 0, 0)
    assert server._derive_rag({"quotedEdges": 5, "candidateQuality": {}}) == (0, 0, 0)


def test_derive_rag_band_negativity() -> None:
    assert server._band_is_negative("positive") is False
    assert server._band_is_negative("0% to -1%") is True
    assert server._band_is_negative("< -5%") is True
    assert server._band_is_negative("1% to 5%") is False


def test_derive_rag_caps_by_executable_when_no_safe_edges() -> None:
    probe = {
        "quotedEdges": 5,
        "candidateQuality": {"marginBands": {"positive": 10}},
        "strategyStats": {"executable_candidates": 3},
    }
    green, amber, red = server._derive_rag(probe)
    assert (green, amber, red) == (3, 7, 0)


# -- auth store -----------------------------------------------------------------


def _authstore(tmp_path: Path) -> Any:
    return server.AuthStore(tmp_path / "auth.json")


def test_authstore_seed_default_creates_hashed_admin(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    path = tmp_path / "auth.json"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    record = json.loads(path.read_text(encoding="utf8"))
    assert record["username"] == "admin"
    assert record["is_default"] is True
    assert record["algo"] == "pbkdf2_hmac_sha256"
    assert record["iterations"] >= 200000
    assert "password" not in record  # only the salted hash is persisted
    assert store.verify("admin", "admin") is True
    assert store.verify("admin", "wrong") is False
    assert store.verify("nope", "admin") is False


def test_authstore_seed_is_idempotent(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    first = json.loads((tmp_path / "auth.json").read_text(encoding="utf8"))
    store.seed_default_if_absent()
    second = json.loads((tmp_path / "auth.json").read_text(encoding="utf8"))
    assert first["salt"] == second["salt"]
    assert first["hash"] == second["hash"]


def test_authstore_change_success_sets_non_default(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    ok, message = store.change("admin", "ops", "new-pass-123")
    assert (ok, message) == (True, "")
    record = json.loads((tmp_path / "auth.json").read_text(encoding="utf8"))
    assert record["username"] == "ops"
    assert record["is_default"] is False
    assert store.verify("ops", "new-pass-123") is True
    assert store.verify("admin", "admin") is False


def test_authstore_change_wrong_current(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    before = (tmp_path / "auth.json").read_text(encoding="utf8")
    ok, message = store.change("bad", None, "new-pass-123")
    assert ok is False
    assert message
    assert (tmp_path / "auth.json").read_text(encoding="utf8") == before
    assert store.verify("admin", "admin") is True


def test_authstore_change_keeps_username_when_new_username_absent(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    ok, _ = store.change("admin", "", "new-pass-123")
    assert ok is True
    record = json.loads((tmp_path / "auth.json").read_text(encoding="utf8"))
    assert record["username"] == "admin"
    assert record["is_default"] is False


def test_authstore_atomic_write_mode_0600(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    store.change("admin", None, "new-pass-123")
    assert ((tmp_path / "auth.json").stat().st_mode & 0o777) == 0o600


def test_verify_password_constant_time_shape(tmp_path: Path) -> None:
    assert server._verify_password({"algo": "other"}, "admin", "admin") is False
    assert (
        server._verify_password(
            {"algo": "pbkdf2_hmac_sha256", "salt": "!!!", "hash": "!!!", "iterations": 1},
            "admin",
            "admin",
        )
        is False
    )


def test_hash_is_not_plaintext(tmp_path: Path) -> None:
    store = _authstore(tmp_path)
    store.seed_default_if_absent()
    record = json.loads((tmp_path / "auth.json").read_text(encoding="utf8"))
    decoded = base64.b64decode(record["hash"])
    assert decoded != b"admin"
    assert len(decoded) == 32


# -- whoami / change (fake handler) ---------------------------------------------


class _AuthHandler:
    """
    Fake handler exercising ``_auth_whoami`` / ``_auth_change`` / ``_authorized``.
    """

    def __init__(
        self,
        state: Any,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.state = state
        self.command = "POST"
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self.rfile = io.BytesIO(body)
        self.sent: dict[str, Any] = {}

    _auth_whoami = server.Handler._auth_whoami
    _auth_change = server.Handler._auth_change
    _authorized = server.Handler._authorized
    _read_body = server.Handler._read_body
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def _seeded_state(tmp_path: Path, **overrides: str) -> tuple[Any, Any]:
    overrides.setdefault("NODEOPS_USER", "")
    config = _config(tmp_path, **overrides)
    authstore = server.AuthStore(config.auth_path)
    authstore.seed_default_if_absent()
    state = server.NodeOpsState(config, object(), object(), auth=authstore)
    return state, authstore


def _change_body(**fields: Any) -> bytes:
    return json.dumps(fields).encode("utf8")


def test_whoami_reports_default_true(tmp_path: Path) -> None:
    state, _ = _seeded_state(tmp_path)
    handler = _AuthHandler(state)
    handler._auth_whoami()
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert handler.sent["payload"] == {"username": "admin", "is_default": True}


def test_whoami_after_change_is_default_false(tmp_path: Path) -> None:
    state, authstore = _seeded_state(tmp_path)
    authstore.change("admin", "ops", "new-pass-123")
    handler = _AuthHandler(state)
    handler._auth_whoami()
    assert handler.sent["payload"] == {"username": "ops", "is_default": False}


def test_auth_change_rejects_short_password(tmp_path: Path) -> None:
    state, _ = _seeded_state(tmp_path)
    body = _change_body(current_password="admin", new_password="short")
    handler = _AuthHandler(state, body)
    handler._auth_change()
    assert handler.sent["status"] == server.HTTPStatus.BAD_REQUEST
    assert "too short" in handler.sent["payload"]["error"]


def test_auth_change_wrong_current_403(tmp_path: Path) -> None:
    state, _ = _seeded_state(tmp_path)
    body = _change_body(current_password="nope", new_password="new-pass-123")
    handler = _AuthHandler(state, body)
    handler._auth_change()
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN


def test_auth_change_success_200(tmp_path: Path) -> None:
    state, authstore = _seeded_state(tmp_path)
    body = _change_body(current_password="admin", new_username="ops", new_password="new-pass-123")
    handler = _AuthHandler(state, body)
    handler._auth_change()
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert handler.sent["payload"] == {"ok": True, "username": "ops", "is_default": False}
    assert authstore.verify("ops", "new-pass-123") is True


def test_auth_change_env_mode_409(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="op", NODEOPS_PASSWORD="pw")
    state = server.NodeOpsState(config, object(), object(), auth=None)
    body = _change_body(current_password="pw", new_password="new-pass-123")
    handler = _AuthHandler(state, body)
    handler._auth_change()
    assert handler.sent["status"] == server.HTTPStatus.CONFLICT


def test_auth_change_disabled_409(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_USER="")
    state = server.NodeOpsState(config, object(), object(), auth=None)
    body = _change_body(current_password="admin", new_password="new-pass-123")
    handler = _AuthHandler(state, body)
    handler._auth_change()
    assert handler.sent["status"] == server.HTTPStatus.CONFLICT


def test_authorized_uses_store_when_no_env(tmp_path: Path) -> None:
    state, _ = _seeded_state(tmp_path)
    good = _AuthHandler(state, headers={"Authorization": _basic_header("admin", "admin")})
    assert good._authorized() is True
    bad = _AuthHandler(state, headers={"Authorization": _basic_header("admin", "wrong")})
    assert bad._authorized() is False
    missing = _AuthHandler(state, headers={})
    assert missing._authorized() is False


# -- /odds store + endpoint -----------------------------------------------------


def test_latest_odds_returns_latest_per_kind(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    store.insert_odds_sample(
        "2026-07-01T00:00:00Z",
        "node-a",
        "topPositiveCandidates",
        [{"instrumentPair": "OLD"}],
    )
    store.insert_odds_sample(
        "2026-07-01T00:01:00Z",
        "node-a",
        "topPositiveCandidates",
        [{"instrumentPair": "NEW"}],
    )
    store.insert_odds_sample(
        "2026-07-01T00:00:30Z",
        "node-a",
        "topNegativeNearMisses",
        [{"instrumentPair": "NEG"}],
    )
    latest = store.latest_odds("node-a")
    assert set(latest) == {"topPositiveCandidates", "topNegativeNearMisses"}
    assert latest["topPositiveCandidates"]["payload"] == [{"instrumentPair": "NEW"}]
    assert latest["topNegativeNearMisses"]["payload"] == [{"instrumentPair": "NEG"}]
    store.close()


def test_latest_odds_empty_for_unknown_node(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    assert store.latest_odds("no-such-node") == {}
    store.close()


def test_sampler_records_value_edge_candidates(tmp_path: Path, monkeypatch: Any) -> None:
    nodes_root = tmp_path / "nodes"
    (nodes_root / "vnode").mkdir(parents=True)
    (nodes_root / "vnode" / "status.json").write_text(
        json.dumps(
            {
                "runtimeProbe": {
                    "candidateQuality": {
                        "topValueEdgeCandidates": [
                            {"instrumentPair": "VAL", "safetyTier": "tier1"},
                        ],
                    },
                },
            },
        ),
        encoding="utf8",
    )
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    monkeypatch.setattr(server, "docker_inspect", lambda name: {"state": "running", "image": "i"})
    monkeypatch.setattr(server, "docker_stats", lambda name: {"mem_mb": 1.0, "cpu_pct": 1.0})

    sampler = server.Sampler(config, store, threading.Event())
    sampler.sample_once()

    kinds = {row["kind"] for row in store._conn.execute("SELECT kind FROM odds_samples")}
    assert "topValueEdgeCandidates" in kinds
    store.close()


class _OddsHandler:
    """
    Fake handler exercising ``_node_odds`` end-to-end against a real store.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "GET"
        self.sent: dict[str, Any] = {}

    _node_odds = server.Handler._node_odds

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def test_node_odds_shape_and_secret_stripping(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    store.insert_odds_sample(
        "2026-07-01T00:00:00Z",
        "node-a",
        "topPositiveCandidates",
        [{"instrumentPair": "X", "CLOUDBET_API_KEY": "leak"}],
    )
    config = _config(tmp_path)
    state = server.NodeOpsState(config, store, object())
    handler = _OddsHandler(state)
    handler._node_odds("node-a")
    assert handler.sent["status"] == server.HTTPStatus.OK
    payload = handler.sent["payload"]
    assert payload["node"] == "node-a"
    candidates = payload["kinds"]["topPositiveCandidates"]["candidates"]
    assert candidates == [{"instrumentPair": "X"}]  # secret key stripped
    store.close()


# -- lifecycle controls gating --------------------------------------------------


class _LifecycleHandler:
    """
    Fake handler exercising ``_node_lifecycle`` / ``_delete_node`` with readonly.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "POST"
        self.headers: dict[str, str] = {}
        self.sent: dict[str, Any] = {}
        self.started: dict[str, Any] | None = None

    _node_lifecycle = server.Handler._node_lifecycle
    _delete_node = server.Handler._delete_node
    _readonly_blocked = server.Handler._readonly_blocked
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}

    def _start_job(self, kind: str, target: str, args: list[str]) -> None:
        self.started = {"kind": kind, "target": target, "args": args}
        self._send_json(server.HTTPStatus.ACCEPTED, {"job_id": "test"})


def test_lifecycle_blocked_in_readonly(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="1")
    state = server.NodeOpsState(config, object(), object())
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "ok")
    for action in ("restart", "stop", "start"):
        handler = _LifecycleHandler(state)
        handler._node_lifecycle("node-a", action)
        assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    delete_handler = _LifecycleHandler(state)
    delete_handler._delete_node("node-a")
    assert delete_handler.started is None
    assert delete_handler.sent["status"] == server.HTTPStatus.FORBIDDEN


def test_lifecycle_ok_calls_docker(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="0")
    state = server.NodeOpsState(config, object(), object())
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "restarted")
    handler = _LifecycleHandler(state)
    handler._node_lifecycle("node-a", "restart")
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert handler.sent["payload"] == {"node": "node-a", "action": "restart", "ok": True}

    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: None)
    fail = _LifecycleHandler(state)
    fail._node_lifecycle("node-a", "stop")
    assert fail.sent["status"] == server.HTTPStatus.BAD_GATEWAY


# -- real HTTP smoke with store-backed auth --------------------------------------


def test_real_http_auth_and_whoami(tmp_path: Path) -> None:
    """
    Drive the real server with store-backed Basic auth over the wire.
    """
    nodes = tmp_path / "nodes"
    (nodes / "demo").mkdir(parents=True)
    (nodes / "demo" / "status.json").write_text(
        '{"runtimeProbe": {"graphEdges": 9, "quotedEdges": 2}}',
        encoding="utf8",
    )
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="1",
        NODEOPS_USER="",
        NODEOPS_NODES_ROOT=str(nodes),
    )
    authstore = server.AuthStore(config.auth_path)
    authstore.seed_default_if_absent()
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    srv = server.build_server(config, store, jobs, authstore)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/api/nodes")
        unauth = conn.getresponse()
        unauth.read()
        assert unauth.status == 401

        auth_header = {"Authorization": _basic_header("admin", "admin")}
        conn.request("GET", "/api/nodes", headers=auth_header)
        nodes_resp = conn.getresponse()
        nodes_body = nodes_resp.read().decode("utf8")
        assert nodes_resp.status == 200, nodes_body
        assert any(node["node"] == "demo" for node in json.loads(nodes_body)["nodes"])

        conn.request("GET", "/api/auth/whoami", headers=auth_header)
        whoami_resp = conn.getresponse()
        whoami = json.loads(whoami_resp.read().decode("utf8"))
        assert whoami_resp.status == 200
        assert whoami == {"username": "admin", "is_default": True}

        change_body = json.dumps(
            {"current_password": "admin", "new_password": "changed-pass-1"},
        ).encode("utf8")
        change_headers = {**auth_header, "Content-Length": str(len(change_body))}
        conn.request("POST", "/api/auth/change", body=change_body, headers=change_headers)
        change_resp = conn.getresponse()
        change = json.loads(change_resp.read().decode("utf8"))
        assert change_resp.status == 200, change
        assert change["ok"] is True

        # The Basic header still authenticates via the cached admin/admin only until
        # the new hash lands; re-auth with the new password for the follow-up call.
        new_header = {"Authorization": _basic_header("admin", "changed-pass-1")}
        conn.request("GET", "/api/auth/whoami", headers=new_header)
        whoami2_resp = conn.getresponse()
        whoami2 = json.loads(whoami2_resp.read().decode("utf8"))
        assert whoami2_resp.status == 200
        assert whoami2["is_default"] is False

        conn.request("GET", "/api/nodes/demo/odds", headers=new_header)
        odds_resp = conn.getresponse()
        odds = json.loads(odds_resp.read().decode("utf8"))
        assert odds_resp.status == 200
        assert odds["node"] == "demo"
        assert isinstance(odds["kinds"], dict)

        # /config requires auth like every other /api route
        config_body = json.dumps({"max_resolution_horizon_hours": 96}).encode("utf8")
        conn.request(
            "POST",
            "/api/nodes/demo/config",
            body=config_body,
            headers={"Content-Length": str(len(config_body))},
        )
        config_unauth = conn.getresponse()
        config_unauth.read()
        assert config_unauth.status == 401
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


# -- node detail exposes uptime + release ----------------------------------------


class _DetailHandler:
    """
    Fake handler exercising ``_node_detail`` against a real store.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "GET"
        self.sent: dict[str, Any] = {}

    _node_detail = server.Handler._node_detail

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def test_node_detail_exposes_uptime_and_release(tmp_path: Path) -> None:
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True)
    (node_dir / "manifest.runtime.json").write_text(
        json.dumps(_validation_manifest()),
        encoding="utf8",
    )
    (node_dir / "release.json").write_text(
        json.dumps({"deployedAt": "2026-07-01T09:00:00Z", "image": "ghcr.io/x/node:abc"}),
        encoding="utf8",
    )
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    store.insert_sample(
        _sample_row(
            "node-a",
            "2026-07-06T12:00:00Z",
            started_at="2026-07-06T10:00:00Z",
            uptime_secs=7200.0,
        ),
    )
    handler = _DetailHandler(server.NodeOpsState(config, store, object()))
    handler._node_detail("node-a")
    payload = handler.sent["payload"]
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert payload["startedAt"] == "2026-07-06T10:00:00Z"
    assert payload["uptimeSecs"] == 7200.0
    assert payload["latest"]["uptime_secs"] == 7200.0
    assert payload["release"]["deployedAt"] == "2026-07-01T09:00:00Z"
    store.close()


# -- market-window config endpoint ------------------------------------------------


def test_validate_config_body() -> None:
    updates, error = server.validate_config_body(
        {
            "max_resolution_horizon_hours": 96,
            "market_timing_filter": "all",
            "instrument_refresh_interval_secs": 120,
            "restart": True,
        },
    )
    assert error == ""
    assert updates == {
        "max_resolution_horizon_hours": 96.0,
        "market_timing_filter": "all",
        "instrument_refresh_interval_secs": 120.0,
    }
    rejected = [
        {},  # no config fields
        {"restart": True},  # restart alone is not a config change
        {"max_resolution_horizon_hours": 0},
        {"max_resolution_horizon_hours": -5},
        {"max_resolution_horizon_hours": 721},
        {"max_resolution_horizon_hours": "96"},
        {"max_resolution_horizon_hours": True},
        {"market_timing_filter": "live_only"},
        {"instrument_refresh_interval_secs": 5},
        {"instrument_refresh_interval_secs": -1},
        {"max_resolution_horizon_hours": 96, "restart": "yes"},
        {"max_resolution_horizon_hours": 96, "auto_execute": True},  # unknown key
        {"validation_mode": False},  # unknown key
    ]
    for body in rejected:
        updates, error = server.validate_config_body(body)
        assert updates is None, body
        assert error, body


class _ConfigHandler:
    """
    Fake handler exercising ``_node_config`` end-to-end against a real node dir.
    """

    def __init__(self, state: Any, body: bytes) -> None:
        self.state = state
        self.command = "POST"
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent: dict[str, Any] = {}

    _node_config = server.Handler._node_config
    _read_body = server.Handler._read_body
    _readonly_blocked = server.Handler._readonly_blocked
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def _window_manifest(**overrides: Any) -> dict[str, Any]:
    manifest = _validation_manifest()
    # the rendered runtime manifest carries the discovery window in the
    # strategy block — the location the strategy actually reads
    manifest["strategy"].update(
        {
            "max_resolution_horizon_hours": 48.0,
            "market_timing_filter": "all",
            "instrument_refresh_interval_secs": 300.0,
        },
    )
    manifest.update(overrides)
    return manifest


def _config_node(
    tmp_path: Path,
    manifest: dict[str, Any] | None = None,
    **config_overrides: str,
) -> tuple[Any, Path]:
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = node_dir / "manifest.runtime.json"
    if manifest is not None:
        manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    config_overrides.setdefault("NODEOPS_NODES_ROOT", str(nodes_root))
    config = _config(tmp_path, **config_overrides)
    state = server.NodeOpsState(config, object(), object())
    return state, manifest_path


def _config_body(**fields: Any) -> bytes:
    return json.dumps(fields).encode("utf8")


def test_config_happy_path_rewrites_manifest_and_restarts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    state, manifest_path = _config_node(tmp_path, _window_manifest())
    docker_calls: list[list[str]] = []

    def _fake_docker(args: list[str], **kwargs: Any) -> str:
        docker_calls.append(args)
        return "node-a\n"

    monkeypatch.setattr(server, "_run_docker", _fake_docker)
    body = _config_body(
        max_resolution_horizon_hours=168,
        market_timing_filter="pre_market",
        restart=True,
    )
    handler = _ConfigHandler(state, body)
    handler._node_config("node-a")

    assert handler.sent["status"] == server.HTTPStatus.OK
    payload = handler.sent["payload"]
    assert payload["ok"] is True
    assert payload["restarted"] is True
    assert payload["changed"]["max_resolution_horizon_hours"] == {"old": 48.0, "new": 168.0}
    assert payload["changed"]["market_timing_filter"] == {"old": "all", "new": "pre_market"}
    assert docker_calls == [["docker", "restart", "--", "node-a"]]

    rewritten = json.loads(manifest_path.read_text(encoding="utf8"))
    assert rewritten["strategy"]["max_resolution_horizon_hours"] == 168.0
    assert rewritten["strategy"]["market_timing_filter"] == "pre_market"
    assert rewritten["strategy"]["instrument_refresh_interval_secs"] == 300.0  # untouched
    assert "max_resolution_horizon_hours" not in rewritten  # no top-level copy invented
    assert server.manifest_is_validation_safe(rewritten) is True

    backup_path = manifest_path.parent / payload["backup"]
    assert backup_path.exists()
    assert payload["backup"].startswith("manifest.runtime.json.bak-")
    backup = json.loads(backup_path.read_text(encoding="utf8"))
    assert backup["strategy"]["max_resolution_horizon_hours"] == 48.0


def test_config_writes_strategy_block_and_mirrors_top_level(tmp_path: Path) -> None:
    """
    Regression: the strategy reads the discovery window from ``strategy.*``, so a
    top-level-only write is silently ignored by the node. Updates must land in the
    strategy block, and a pre-existing top-level copy must be kept in sync rather
    than left stale and contradicting.
    """
    manifest = _window_manifest(max_resolution_horizon_hours=48.0)  # stale top-level copy
    state, manifest_path = _config_node(tmp_path, manifest)
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=168))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert handler.sent["payload"]["changed"]["max_resolution_horizon_hours"] == {
        "old": 48.0,
        "new": 168.0,
    }
    rewritten = json.loads(manifest_path.read_text(encoding="utf8"))
    assert rewritten["strategy"]["max_resolution_horizon_hours"] == 168.0
    assert rewritten["max_resolution_horizon_hours"] == 168.0  # mirrored, not stale


def test_config_no_restart_leaves_docker_alone(tmp_path: Path, monkeypatch: Any) -> None:
    state, manifest_path = _config_node(tmp_path, _window_manifest())

    def _explode(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("docker must not be invoked when restart is false")

    monkeypatch.setattr(server, "_run_docker", _explode)
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert handler.sent["payload"]["restarted"] is False
    rewritten = json.loads(manifest_path.read_text(encoding="utf8"))
    assert rewritten["strategy"]["max_resolution_horizon_hours"] == 96.0


def test_config_rejects_unknown_keys_400(tmp_path: Path) -> None:
    state, manifest_path = _config_node(tmp_path, _window_manifest())
    before = manifest_path.read_text(encoding="utf8")
    for body in (
        _config_body(max_resolution_horizon_hours=96, auto_execute=True),
        _config_body(live_execution_armed=True),
        _config_body(validation_mode=False),
    ):
        handler = _ConfigHandler(state, body)
        handler._node_config("node-a")
        assert handler.sent["status"] == server.HTTPStatus.BAD_REQUEST
        assert "unknown keys" in handler.sent["payload"]["error"]
    assert manifest_path.read_text(encoding="utf8") == before
    assert list(manifest_path.parent.glob("*.bak-*")) == []


def test_config_rejects_invalid_values_400(tmp_path: Path) -> None:
    state, _ = _config_node(tmp_path, _window_manifest())
    for body in (
        _config_body(max_resolution_horizon_hours=0),
        _config_body(max_resolution_horizon_hours=1000),
        _config_body(market_timing_filter="everything"),
        _config_body(instrument_refresh_interval_secs=1),
        _config_body(restart=True),
    ):
        handler = _ConfigHandler(state, body)
        handler._node_config("node-a")
        assert handler.sent["status"] == server.HTTPStatus.BAD_REQUEST


def test_config_readonly_403(tmp_path: Path) -> None:
    state, manifest_path = _config_node(tmp_path, _window_manifest(), NODEOPS_READONLY="1")
    before = manifest_path.read_text(encoding="utf8")
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    assert manifest_path.read_text(encoding="utf8") == before


def test_config_missing_manifest_404(tmp_path: Path) -> None:
    state, _ = _config_node(tmp_path, manifest=None)
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.NOT_FOUND


def test_config_unsafe_resulting_manifest_403(tmp_path: Path) -> None:
    """
    Validation-safety regression: a manifest that is not (or would not stay)
    validation-safe is refused before any backup or write happens.
    """
    unsafe = _window_manifest()
    unsafe["strategy"] = {"auto_execute": True}
    state, manifest_path = _config_node(tmp_path, unsafe)
    before = manifest_path.read_text(encoding="utf8")
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    assert "validation-safe" in handler.sent["payload"]["error"]
    assert manifest_path.read_text(encoding="utf8") == before  # untouched
    assert list(manifest_path.parent.glob("*.bak-*")) == []  # no backup written


def test_config_restart_failure_502_after_write(tmp_path: Path, monkeypatch: Any) -> None:
    state, manifest_path = _config_node(tmp_path, _window_manifest())
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: None)
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96, restart=True))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.BAD_GATEWAY
    payload = handler.sent["payload"]
    assert "restart failed" in payload["error"]
    # the manifest write already happened; the response still reports it
    assert payload["changed"]["max_resolution_horizon_hours"]["new"] == 96.0
    rewritten = json.loads(manifest_path.read_text(encoding="utf8"))
    assert rewritten["strategy"]["max_resolution_horizon_hours"] == 96.0


def test_config_preserves_manifest_owner_and_mode(tmp_path: Path, monkeypatch: Any) -> None:
    """
    The rewrite must keep the manifest (and its backup) owned by whoever owned the
    manifest and with the same mode. nodeops runs as root but the deploy pipeline runs
    as ``ubuntu`` and must still be able to overwrite the file, so the root-created
    replacement is chowned/chmodded back to the original owner.

    Cross-uid restore needs root, so this exercises the same-user path: the mode
    is set to a value that differs from both ``mkstemp``'s 0o600 default and the
    umask default, proving the captured mode is genuinely restored and never
    corrupted.

    """
    state, manifest_path = _config_node(tmp_path, _window_manifest())
    manifest_path.chmod(0o640)
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "node-a\n")
    before = manifest_path.stat()

    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.OK

    after = manifest_path.stat()
    assert after.st_uid == before.st_uid
    assert after.st_gid == before.st_gid
    assert (after.st_mode & 0o777) == (before.st_mode & 0o777) == 0o640

    backup_path = manifest_path.parent / handler.sent["payload"]["backup"]
    backup = backup_path.stat()
    assert backup.st_uid == before.st_uid
    assert backup.st_gid == before.st_gid
    assert (backup.st_mode & 0o777) == (before.st_mode & 0o777)


def test_config_chowns_manifest_and_backup_to_original_owner(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """
    Even where the test process cannot actually chown (non-root CI), assert the
    intent: ``os.chown`` is invoked for both the rewritten manifest and its
    backup with the *original* owner's uid/gid captured before the write.
    """
    state, manifest_path = _config_node(tmp_path, _window_manifest())
    manifest_path.chmod(0o644)
    before = manifest_path.stat()

    chown_calls: list[tuple[str, int, int]] = []

    def _record_chown(path: Any, uid: int, gid: int) -> None:
        chown_calls.append((str(path), uid, gid))

    monkeypatch.setattr(server.os, "chown", _record_chown)
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "node-a\n")

    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    assert handler.sent["status"] == server.HTTPStatus.OK

    backup_path = manifest_path.parent / handler.sent["payload"]["backup"]
    assert (str(manifest_path), before.st_uid, before.st_gid) in chown_calls
    assert (str(backup_path), before.st_uid, before.st_gid) in chown_calls


# -- real HTTP smoke for /config --------------------------------------------------


def test_real_http_config_endpoint(tmp_path: Path) -> None:
    """
    Drive ``POST /api/nodes/<name>/config`` over the wire against a temp node dir.
    """
    nodes = tmp_path / "nodes"
    (nodes / "demo").mkdir(parents=True)
    manifest_path = nodes / "demo" / "manifest.runtime.json"
    manifest_path.write_text(json.dumps(_window_manifest()), encoding="utf8")
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="0",
        NODEOPS_NODES_ROOT=str(nodes),
    )
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    srv = server.build_server(config, store, jobs)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        good = json.dumps(
            {
                "max_resolution_horizon_hours": 96,
                "market_timing_filter": "pre_market",
                "restart": False,
            },
        ).encode("utf8")
        conn.request(
            "POST",
            "/api/nodes/demo/config",
            body=good,
            headers={"Content-Length": str(len(good))},
        )
        ok_resp = conn.getresponse()
        ok = json.loads(ok_resp.read().decode("utf8"))
        assert ok_resp.status == 200, ok
        assert ok["ok"] is True
        assert ok["restarted"] is False
        assert ok["changed"]["max_resolution_horizon_hours"] == {"old": 48.0, "new": 96.0}

        rewritten = json.loads(manifest_path.read_text(encoding="utf8"))
        assert rewritten["strategy"]["max_resolution_horizon_hours"] == 96.0
        assert rewritten["strategy"]["market_timing_filter"] == "pre_market"
        backup_path = manifest_path.parent / ok["backup"]
        assert backup_path.exists()
        backup = json.loads(backup_path.read_text(encoding="utf8"))
        assert backup["strategy"]["max_resolution_horizon_hours"] == 48.0

        bad = json.dumps({"max_resolution_horizon_hours": 96, "auto_execute": True}).encode("utf8")
        conn.request(
            "POST",
            "/api/nodes/demo/config",
            body=bad,
            headers={"Content-Length": str(len(bad))},
        )
        bad_resp = conn.getresponse()
        rejected = json.loads(bad_resp.read().decode("utf8"))
        assert bad_resp.status == 400, rejected
        assert "unknown keys" in rejected["error"]
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


# -- FEATURE 1: alerting --------------------------------------------------------


def _alert_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "quoted_edges": 0,
        "heartbeat_age_secs": 0.0,
        "container_state": "running",
        "cross_venue_candidate_count": 0,
    }
    row.update(overrides)
    return row


def _capture_webhook(sink: list[Any], *, key: str = "both") -> Any:
    """
    Return a ``post_webhook`` stub that records calls into ``sink`` and returns True.
    """

    def _stub(url: str, payload: dict[str, Any]) -> bool:
        if key == "url":
            sink.append(url)
        elif key == "payload":
            sink.append(payload)
        else:
            sink.append((url, payload))
        return True

    return _stub


def test_detect_transitions_first_sample_is_silent(tmp_path: Path) -> None:
    curr = server._alert_node_state(_alert_row(quoted_edges=5), 180.0)
    # No prior state ⇒ baseline only, nothing fires.
    assert server.detect_transitions(None, curr) == []


def test_detect_transitions_quoting_stopped_and_started(tmp_path: Path) -> None:
    quoting = server._alert_node_state(_alert_row(quoted_edges=5), 180.0)
    idle = server._alert_node_state(_alert_row(quoted_edges=0), 180.0)
    stopped = {e["condition"] for e in server.detect_transitions(quoting, idle)}
    assert "quoting_stopped" in stopped
    started = {e["condition"] for e in server.detect_transitions(idle, quoting)}
    assert "quoting_started" in started


def test_detect_transitions_heartbeat_stale(tmp_path: Path) -> None:
    fresh = server._alert_node_state(_alert_row(heartbeat_age_secs=10.0), 180.0)
    stale = server._alert_node_state(_alert_row(heartbeat_age_secs=999.0), 180.0)
    conds = {e["condition"] for e in server.detect_transitions(fresh, stale)}
    assert "heartbeat_stale" in conds
    # once stale, staying stale must not re-fire
    assert server.detect_transitions(stale, stale) == []


def test_detect_transitions_container_left_running(tmp_path: Path) -> None:
    up = server._alert_node_state(_alert_row(container_state="running"), 180.0)
    down = server._alert_node_state(_alert_row(container_state="exited"), 180.0)
    conds = {e["condition"] for e in server.detect_transitions(up, down)}
    assert "container_left_running" in conds


def test_detect_transitions_cross_venue_is_high_severity(tmp_path: Path) -> None:
    none = server._alert_node_state(_alert_row(cross_venue_candidate_count=0), 180.0)
    found = server._alert_node_state(_alert_row(cross_venue_candidate_count=2), 180.0)
    events = server.detect_transitions(none, found)
    match = [e for e in events if e["condition"] == "cross_venue_candidate"]
    assert len(match) == 1
    assert match[0]["severity"] == "high"


def test_alertstate_dedupes_on_unchanged_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    alerts = server.AlertState(config)
    row = _alert_row(quoted_edges=5)
    assert alerts.observe("node-a", row) == []  # first sample: baseline, silent
    assert alerts.observe("node-a", row) == []  # unchanged: no re-fire
    idle = _alert_row(quoted_edges=0)
    fired = alerts.observe("node-a", idle)
    assert {e["condition"] for e in fired} == {"quoting_stopped"}
    # staying idle does not re-fire
    assert alerts.observe("node-a", idle) == []


def test_alertstate_posts_to_webhook(tmp_path: Path, monkeypatch: Any) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(server, "post_webhook", _capture_webhook(posted))
    config = _config(tmp_path, NODEOPS_ALERT_WEBHOOK="https://hook.example/notify")
    alerts = server.AlertState(config)
    alerts.observe("node-a", _alert_row(quoted_edges=5))  # baseline
    alerts.observe("node-a", _alert_row(quoted_edges=0))  # quoting_stopped
    assert len(posted) == 1
    url, payload = posted[0]
    assert url == "https://hook.example/notify"
    assert payload["condition"] == "quoting_stopped"
    assert payload["node"] == "node-a"


def test_alertstate_no_webhook_no_post(tmp_path: Path, monkeypatch: Any) -> None:
    called: list[Any] = []
    monkeypatch.setattr(server, "post_webhook", lambda url, payload: called.append(url))
    config = _config(tmp_path)  # NODEOPS_ALERT_WEBHOOK unset
    alerts = server.AlertState(config)
    alerts.observe("node-a", _alert_row(quoted_edges=5))
    alerts.observe("node-a", _alert_row(quoted_edges=0))
    assert called == []  # nothing posted when no webhook configured


def test_alertstate_ring_is_bounded(tmp_path: Path) -> None:
    config = _config(tmp_path)
    alerts = server.AlertState(config)
    alerts.observe("n", _alert_row(quoted_edges=0))  # baseline
    for i in range(server.ALERT_RING_SIZE + 50):
        # flip quoting each iteration to force one transition per observe
        q = 5 if i % 2 == 0 else 0
        alerts.observe("n", _alert_row(quoted_edges=q))
    recent = alerts.recent(server.ALERT_RING_SIZE + 100)
    assert len(recent) == server.ALERT_RING_SIZE
    # newest-first ordering
    assert recent[0]["ts_utc"] >= recent[-1]["ts_utc"]


def test_post_webhook_swallows_errors(tmp_path: Path, monkeypatch: Any) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise server.urllib.error.URLError("down")

    monkeypatch.setattr(server.urllib.request, "urlopen", _boom)
    # must return False, not raise
    assert server.post_webhook("https://hook.example/x", {"a": 1}) is False


def test_sampler_fires_alert_on_quoting_stop(tmp_path: Path, monkeypatch: Any) -> None:
    nodes_root = tmp_path / "nodes"
    (nodes_root / "n").mkdir(parents=True)
    status_path = nodes_root / "n" / "status.json"
    status_path.write_text('{"runtimeProbe": {"quotedEdges": 5}}', encoding="utf8")
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    monkeypatch.setattr(server, "docker_inspect", lambda name: {"state": "running", "image": "i"})
    monkeypatch.setattr(server, "docker_stats", lambda name: {"mem_mb": 1.0, "cpu_pct": 1.0})
    alerts = server.AlertState(config)
    sampler = server.Sampler(config, store, threading.Event(), alerts)
    sampler.sample_once()  # baseline (quoting)
    status_path.write_text('{"runtimeProbe": {"quotedEdges": 0}}', encoding="utf8")
    sampler.sample_once()  # quoting stops → alert
    conds = {e["condition"] for e in alerts.recent(50)}
    assert "quoting_stopped" in conds
    store.close()


class _AlertHandler:
    """
    Fake handler exercising the alert routes against a real AlertState.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "POST"
        self.headers: dict[str, str] = {}
        self.sent: dict[str, Any] = {}

    _list_alerts = server.Handler._list_alerts
    _alerts_test = server.Handler._alerts_test
    _mask_webhook = server.Handler._mask_webhook
    _readonly_blocked = server.Handler._readonly_blocked
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def test_alerts_test_endpoint_fires_and_masks_webhook(tmp_path: Path, monkeypatch: Any) -> None:
    posted: list[Any] = []
    monkeypatch.setattr(server, "post_webhook", _capture_webhook(posted, key="url"))
    config = _config(
        tmp_path,
        NODEOPS_READONLY="0",
        NODEOPS_ALERT_WEBHOOK="https://hook.example/secret/path?token=abc",
    )
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _AlertHandler(state)
    handler._alerts_test()
    assert handler.sent["status"] == server.HTTPStatus.OK
    payload = handler.sent["payload"]
    assert payload["ok"] is True
    assert payload["webhook_configured"] is True
    assert payload["webhook"] == "https://hook.example/…"  # host only, path/token masked
    assert posted == ["https://hook.example/secret/path?token=abc"]
    store.close()


def test_alerts_test_blocked_in_readonly(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="1")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _AlertHandler(state)
    handler._alerts_test()
    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    store.close()


def test_list_alerts_returns_ring(tmp_path: Path) -> None:
    config = _config(tmp_path)
    alerts = server.AlertState(config)
    alerts.observe("n", _alert_row(quoted_edges=5))
    alerts.observe("n", _alert_row(quoted_edges=0))
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object(), alerts=alerts)
    handler = _AlertHandler(state)
    handler.command = "GET"
    handler._list_alerts()
    assert handler.sent["status"] == server.HTTPStatus.OK
    payload = handler.sent["payload"]
    assert payload["webhook_configured"] is False
    assert payload["webhook"] is None
    assert any(a["condition"] == "quoting_stopped" for a in payload["alerts"])
    store.close()


# -- FEATURE 2: audit log -------------------------------------------------------


class _AuditListHandler:
    """
    Fake handler exercising ``_list_audit`` against a real store.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "GET"
        self.sent: dict[str, Any] = {}

    _list_audit = server.Handler._list_audit

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def test_audit_table_created_and_roundtrip(tmp_path: Path) -> None:
    store = server.Store(tmp_path / "nodeops.db")
    tables = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "audit_log" in tables
    store.insert_audit("2026-07-01T00:00:00Z", "admin", "restart", "node-a", "{}", "ok")
    rows = store.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["username"] == "admin"
    assert rows[0]["action"] == "restart"
    assert rows[0]["node"] == "node-a"
    assert rows[0]["status"] == "ok"
    store.close()


def test_audit_created_on_existing_db_without_table(tmp_path: Path) -> None:
    """
    A DB created before audit_log existed gains it via CREATE TABLE IF NOT EXISTS.
    """
    db_path = tmp_path / "nodeops.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{column} TEXT" for column in server.SAMPLE_COLUMNS)
        + ")",
    )
    conn.commit()
    conn.close()
    store = server.Store(db_path)
    tables = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "audit_log" in tables
    store.close()


def test_lifecycle_writes_audit_row(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="0")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "restarted")
    handler = _LifecycleHandler(state)
    handler.headers = {"Authorization": _basic_header("opsuser", "pw")}
    handler._node_lifecycle("node-a", "restart")
    rows = store.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "restart"
    assert rows[0]["node"] == "node-a"
    assert rows[0]["username"] == "opsuser"
    assert rows[0]["status"] == "ok"
    store.close()


def test_config_writes_audit_row_with_params(tmp_path: Path) -> None:
    manifest = _window_manifest()
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True)
    (node_dir / "manifest.runtime.json").write_text(json.dumps(manifest), encoding="utf8")
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root), NODEOPS_READONLY="0")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _ConfigHandler(state, _config_body(max_resolution_horizon_hours=96))
    handler._node_config("node-a")
    rows = store.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "config"
    assert "96" in rows[0]["params_summary"]
    store.close()


def test_delete_writes_audit_row(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="0")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _LifecycleHandler(state)
    handler.headers = {}
    handler._delete_node("node-a")
    rows = store.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "delete"
    assert rows[0]["status"] == "accepted"
    store.close()


def test_readonly_action_audited_as_blocked_not_executed(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="1")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    calls: list[Any] = []
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: calls.append(a))
    handler = _LifecycleHandler(state)
    handler.headers = {}
    handler._node_lifecycle("node-a", "stop")
    assert calls == []  # docker never invoked
    rows = store.recent_audit(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "stop"
    assert rows[0]["status"] == "blocked"  # audited as blocked, not as executed
    store.close()


def test_audit_strips_secrets_from_params(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _AlertHandler(state)  # any handler with _audit bound
    handler.headers = {}
    handler._audit("test", "node-a", {"image": "ghcr/x", "CLOUDBET_API_KEY": "leak"}, "ok")
    rows = store.recent_audit(10)
    assert "leak" not in rows[0]["params_summary"]
    assert "image" in rows[0]["params_summary"]
    store.close()


def test_list_audit_endpoint_returns_rows_and_limit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = server.Store(config.db_path)
    for i in range(5):
        store.insert_audit(f"2026-07-01T00:00:0{i}Z", "admin", "restart", f"n{i}", "{}", "ok")
    state = server.NodeOpsState(config, store, object())
    handler = _AuditListHandler(state)
    handler._list_audit({"limit": ["2"]})
    assert handler.sent["status"] == server.HTTPStatus.OK
    payload = handler.sent["payload"]
    assert payload["limit"] == 2
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["node"] == "n4"  # newest first
    store.close()


# -- FEATURE 3: devig diagnostics panel -----------------------------------------


def test_node_detail_surfaces_devig_and_odds_stats(tmp_path: Path) -> None:
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text(
        json.dumps(
            {
                "runtimeProbe": {
                    "candidateQuality": {
                        "devigDiagnostics": {
                            "overround": "1.05",
                            "vig": "0.048",
                            "methodCounts": {"multiplicative": 12},
                        },
                    },
                },
            },
        ),
        encoding="utf8",
    )
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    store.insert_odds_sample("2026-07-01T00:00:00Z", "node-a", "topPositiveCandidates", [{"x": 1}])
    store.insert_odds_sample("2026-07-01T00:05:00Z", "node-a", "topPositiveCandidates", [{"x": 2}])
    handler = _DetailHandler(server.NodeOpsState(config, store, object()))
    handler._node_detail("node-a")
    payload = handler.sent["payload"]
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert payload["devigDiagnostics"]["overround"] == "1.05"
    assert payload["oddsSamples"]["count"] == 2
    assert payload["oddsSamples"]["latestTs"] == "2026-07-01T00:05:00Z"
    store.close()


def test_node_detail_devig_graceful_empty(tmp_path: Path) -> None:
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text('{"runtimeProbe": {"graphEdges": 3}}', encoding="utf8")
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    handler = _DetailHandler(server.NodeOpsState(config, store, object()))
    handler._node_detail("node-a")
    payload = handler.sent["payload"]
    assert payload["devigDiagnostics"] is None
    assert payload["oddsSamples"] == {"count": 0, "latestTs": None}
    store.close()


# -- real HTTP smoke for alerts / audit -----------------------------------------


def test_real_http_alerts_audit_smoke(tmp_path: Path, monkeypatch: Any) -> None:
    """
    Wire-level smoke: a synthetic quoting-stopped alert POSTs the webhook, a
    (mocked-docker) restart writes an audit row, and /api/alerts/test fires.
    """
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "post_webhook", _capture_webhook(posted, key="payload"))
    monkeypatch.setattr(server, "_run_docker", lambda *a, **k: "restarted")

    nodes = tmp_path / "nodes"
    (nodes / "demo").mkdir(parents=True)
    status_path = nodes / "demo" / "status.json"
    status_path.write_text('{"runtimeProbe": {"quotedEdges": 5}}', encoding="utf8")
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="0",
        NODEOPS_NODES_ROOT=str(nodes),
        NODEOPS_ALERT_WEBHOOK="https://hook.example/notify",
    )
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    alerts = server.AlertState(config)
    monkeypatch.setattr(server, "docker_inspect", lambda name: {"state": "running", "image": "i"})
    monkeypatch.setattr(server, "docker_stats", lambda name: {"mem_mb": 1.0, "cpu_pct": 1.0})
    # drive two samples to force a quoting-stopped transition (webhook attempted)
    sampler = server.Sampler(config, store, threading.Event(), alerts)
    sampler.sample_once()
    status_path.write_text('{"runtimeProbe": {"quotedEdges": 0}}', encoding="utf8")
    sampler.sample_once()
    assert any(p["condition"] == "quoting_stopped" for p in posted)

    srv = server.build_server(config, store, jobs, None, alerts)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        # a mocked-docker restart writes an audit row
        conn.request("POST", "/api/nodes/demo/restart")
        restart_resp = conn.getresponse()
        assert restart_resp.status == 200, restart_resp.read()
        restart_resp.read()

        conn.request("GET", "/api/audit")
        audit_resp = conn.getresponse()
        audit = json.loads(audit_resp.read().decode("utf8"))
        assert audit_resp.status == 200
        assert any(r["action"] == "restart" and r["node"] == "demo" for r in audit["rows"])

        # /api/alerts/test fires a synthetic alert to the webhook
        before = len(posted)
        conn.request("POST", "/api/alerts/test")
        test_resp = conn.getresponse()
        test = json.loads(test_resp.read().decode("utf8"))
        assert test_resp.status == 200
        assert test["ok"] is True
        assert test["webhook"] == "https://hook.example/…"
        assert len(posted) == before + 1

        # /api/alerts lists the ring, webhook masked
        conn.request("GET", "/api/alerts")
        alerts_resp = conn.getresponse()
        listing = json.loads(alerts_resp.read().decode("utf8"))
        assert alerts_resp.status == 200
        assert listing["webhook_configured"] is True
        assert listing["webhook"] == "https://hook.example/…"
        assert any(a["condition"] == "quoting_stopped" for a in listing["alerts"])
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


# -- manual execution-approval queue ----------------------------------------------


def _approvals_status(approval_id: str = "abc123def456") -> dict[str, Any]:
    return {
        "runtimeProbe": {
            "executionApprovals": {
                "mode": "manual",
                "max_pending": 10,
                "ttl_secs": 300.0,
                "staged": 1,
                "pending": [
                    {
                        "approval_id": approval_id,
                        "venue_a": "CLOUDBET",
                        "venue_b": "SXBET",
                        "odds_a": "2.20",
                        "odds_b": "2.20",
                        "stake_a": "12.5",
                        "stake_b": "12.5",
                        "expected_profit": "2.5",
                        "expires_at": "2026-07-12T00:05:00Z",
                    },
                ],
                "recent_decisions": [],
            },
        },
    }


def test_execution_approvals_probe_extraction() -> None:
    probe = _approvals_status()["runtimeProbe"]
    assert server.execution_approvals_from_probe(probe)["mode"] == "manual"
    legacy = {"strategyStats": {"execution_approvals": {"mode": "manual", "pending": []}}}
    assert server.execution_approvals_from_probe(legacy)["pending"] == []
    assert server.execution_approvals_from_probe(None) is None
    assert server.execution_approvals_from_probe({}) is None


def test_node_detail_includes_execution_approvals(tmp_path: Path) -> None:
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "node-a"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text(json.dumps(_approvals_status()), encoding="utf8")
    config = _config(tmp_path, NODEOPS_NODES_ROOT=str(nodes_root))
    store = server.Store(config.db_path)
    handler = _DetailHandler(server.NodeOpsState(config, store, object()))
    handler._node_detail("node-a")
    payload = handler.sent["payload"]
    assert handler.sent["status"] == server.HTTPStatus.OK
    assert payload["executionApprovals"]["mode"] == "manual"
    assert payload["executionApprovals"]["pending"][0]["approval_id"] == "abc123def456"
    store.close()


class _ApprovalHandler:
    """
    Fake handler exercising the approvals routes with readonly + audit bound.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.command = "POST"
        self.headers: dict[str, str] = {}
        self.sent: dict[str, Any] = {}

    _route_approvals = server.Handler._route_approvals
    _node_approvals = server.Handler._node_approvals
    _approval_action = server.Handler._approval_action
    _readonly_blocked = server.Handler._readonly_blocked
    _audit = server.Handler._audit
    _current_username = server.Handler._current_username

    def _send_json(self, status: int, payload: Any) -> None:
        self.sent = {"status": status, "payload": payload}


def test_approval_action_blocked_in_readonly(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="1")
    store = server.Store(config.db_path)
    state = server.NodeOpsState(config, store, object())
    handler = _ApprovalHandler(state)

    handler._approval_action("node-a", "abc123def456", "approve")

    assert handler.sent["status"] == server.HTTPStatus.FORBIDDEN
    assert not (config.nodes_root / "node-a" / server.COMMANDS_DIRNAME).exists()
    rows = store.recent_audit(5)
    assert rows[0]["action"] == "approve_arb"
    assert rows[0]["status"] == "blocked"
    store.close()


def test_approval_action_rejects_invalid_id(tmp_path: Path) -> None:
    config = _config(tmp_path, NODEOPS_READONLY="0")
    store = server.Store(config.db_path)
    handler = _ApprovalHandler(server.NodeOpsState(config, store, object()))

    handler._approval_action("node-a", "NOT-HEX", "approve")

    assert handler.sent["status"] == server.HTTPStatus.BAD_REQUEST
    assert not (config.nodes_root / "node-a" / server.COMMANDS_DIRNAME).exists()
    store.close()


def test_real_http_approvals_get_and_actions(tmp_path: Path) -> None:
    """
    Drive GET/POST approvals over real HTTP: command files land in the node dir,
    unknown/expired ids 404 without writing, and every decision is audited.
    """
    nodes = tmp_path / "nodes"
    node_dir = nodes / "demo"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text(json.dumps(_approvals_status()), encoding="utf8")
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="0",
        NODEOPS_NODES_ROOT=str(nodes),
    )
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    srv = server.build_server(config, store, jobs)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/api/nodes/demo/approvals")
        listing_resp = conn.getresponse()
        listing = json.loads(listing_resp.read().decode("utf8"))
        assert listing_resp.status == 200, listing
        assert listing["readonly"] is False
        assert listing["approvals"]["pending"][0]["approval_id"] == "abc123def456"

        conn.request("POST", "/api/nodes/demo/approvals/abc123def456/approve")
        approve_resp = conn.getresponse()
        approve = json.loads(approve_resp.read().decode("utf8"))
        assert approve_resp.status == 202, approve
        assert approve["ok"] is True
        command_files = sorted((node_dir / server.COMMANDS_DIRNAME).glob("*.json"))
        assert len(command_files) == 1
        command = json.loads(command_files[0].read_text(encoding="utf8"))
        assert command["command"] == "approve_arb"
        assert command["approval_id"] == "abc123def456"
        assert command["id"] == approve["command_id"]

        conn.request("POST", "/api/nodes/demo/approvals/abc123def456/reject")
        reject_resp = conn.getresponse()
        reject = json.loads(reject_resp.read().decode("utf8"))
        assert reject_resp.status == 202, reject
        command_files = sorted((node_dir / server.COMMANDS_DIRNAME).glob("*.json"))
        assert len(command_files) == 2
        reject_command = json.loads(command_files[-1].read_text(encoding="utf8"))
        assert reject_command["command"] == "reject_arb"

        conn.request("POST", "/api/nodes/demo/approvals/eeeeeeeeeeee/approve")
        unknown_resp = conn.getresponse()
        unknown_resp.read()
        assert unknown_resp.status == 404
        assert len(sorted((node_dir / server.COMMANDS_DIRNAME).glob("*.json"))) == 2

        rows = store.recent_audit(10)
        assert [(row["action"], row["status"]) for row in rows[:3]] == [
            ("approve_arb", "unknown_approval"),
            ("reject_arb", "accepted"),
            ("approve_arb", "accepted"),
        ]
        assert "abc123def456" in rows[1]["params_summary"]
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


def test_real_http_approval_action_requires_auth(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes"
    node_dir = nodes / "demo"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text(json.dumps(_approvals_status()), encoding="utf8")
    config = _config(
        tmp_path,
        NODEOPS_HOST="127.0.0.1",
        NODEOPS_PORT="0",
        NODEOPS_READONLY="0",
        NODEOPS_USER="",
        NODEOPS_NODES_ROOT=str(nodes),
    )
    authstore = server.AuthStore(config.auth_path)
    authstore.seed_default_if_absent()
    store = server.Store(config.db_path)
    jobs = server.Jobs()
    srv = server.build_server(config, store, jobs, authstore)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("POST", "/api/nodes/demo/approvals/abc123def456/approve")
        unauth_resp = conn.getresponse()
        unauth_resp.read()
        assert unauth_resp.status == 401
        assert not (node_dir / server.COMMANDS_DIRNAME).exists()

        auth_header = {"Authorization": _basic_header("admin", "admin")}
        conn.request(
            "POST",
            "/api/nodes/demo/approvals/abc123def456/approve",
            headers=auth_header,
        )
        approve_resp = conn.getresponse()
        approve = json.loads(approve_resp.read().decode("utf8"))
        assert approve_resp.status == 202, approve
        command_files = sorted((node_dir / server.COMMANDS_DIRNAME).glob("*.json"))
        assert len(command_files) == 1
        command = json.loads(command_files[0].read_text(encoding="utf8"))
        assert command["requested_by"] == "admin"
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()
