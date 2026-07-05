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
            "candidateQuality": {"ragBands": {"green": 4, "amber": 2, "red": 1}},
            "strategyStats": {
                "raw_arbitrage_detections": 100,
                "opportunities_found": 12,
                "executable_candidates": 6,
                "opportunities_executed": 2,
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
    assert row["heartbeat_age_secs"] == 60.0
    assert row["container_state"] == "running"


def test_build_sample_row_defaults_missing_probe_to_zero() -> None:
    now = datetime.now(UTC)
    row = server.build_sample_row("node-a", None, None, None, {}, now)
    assert row["graph_edges"] == 0
    assert row["cross_venue_candidate_count"] == 0
    assert row["heartbeat_age_secs"] is None
    assert row["mem_mb"] is None


# -- docker stats parsing (no live docker) --------------------------------------


def test_parse_mem_and_percent() -> None:
    assert server._parse_mem_mb("512MiB / 4GiB") == 512.0
    assert server._parse_mem_mb("1.5GiB / 4GiB") == 1536.0
    assert server._parse_percent("12.50%") == 12.5
    assert server._parse_mem_mb("garbage") is None


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
