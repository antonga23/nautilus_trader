"""
Self-contained nodeops dashboard server.

Runs on the strategy-node deploy host and exposes a browser view plus JSON API to
list, query, and manage deployed betting-arbitrage trading nodes. A background
sampler thread reads each node's ``status.json`` (``runtimeProbe``),
``heartbeat.json``, and ``docker inspect``/``docker stats`` output on an interval
and appends a row to a local SQLite database so the #210 cross-venue gate metrics
can be queried over time.

Python standard library only: ``http.server``/``socketserver`` for the server,
``sqlite3`` for storage, ``subprocess`` for the docker CLI, ``base64`` for HTTP
Basic auth. No third-party dependencies so the deploy host needs nothing extra.

The API never reads or returns venue credential env vars; deploys reuse the host's
existing env file by path (``NODEOPS_ENV_FILE``) and manifests are already
secrets-free, but any key matching a secret pattern is stripped defensively before
JSON is returned.

"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit


# datetime.UTC is a 3.11+ symbol, but the strategy-node deploy host runs Python 3.10;
# fall back to the equivalent timezone.utc there. (ruff UP017 targets py3.12 and would
# rewrite the fallback back to datetime.UTC, so it is silenced on that one line.)
try:
    from datetime import UTC
except ImportError:  # pragma: no cover - Python 3.10 deploy host
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017


logger = logging.getLogger("nodeops")

# Node/container/image identifiers must start alphanumeric (matches docker's own
# rule) so a value can never be read as a CLI flag (``-f``) or a path segment
# (``.``/``..``) — see valid_name/valid_image.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")
SECRET_KEY_PATTERN = re.compile(
    r"(API_KEY|PRIVATE_KEY|PASSWORD|TOKEN|SECRET|CREDENTIAL)",
    re.IGNORECASE,
)
MANIFEST_SUFFIX = ".json"
MANIFEST_PREFIX = "deploy/strategy_nodes/"
# Cap request bodies: deploy/lifecycle payloads are tiny JSON, so refuse anything
# larger rather than buffering an attacker-supplied Content-Length into memory.
MAX_BODY_BYTES = 1 << 20

SAMPLE_COLUMNS = (
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
    "mem_mb",
    "cpu_pct",
)
# Numeric metric columns the history endpoint may chart.
HISTORY_METRICS = frozenset(
    {
        "heartbeat_age_secs",
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
        "mem_mb",
        "cpu_pct",
    },
)


class Config:
    """
    Runtime configuration resolved from ``NODEOPS_*`` environment variables.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        # Default to loopback: exposing the dashboard beyond localhost is an explicit
        # operator choice (NODEOPS_HOST=0.0.0.0) and main() refuses that without auth.
        self.host = env.get("NODEOPS_HOST", "127.0.0.1")
        self.port = int(env.get("NODEOPS_PORT", "8090"))
        self.nodes_root = Path(env.get("NODEOPS_NODES_ROOT", "/opt/cloudbet/strategy-nodes"))
        self.db_path = Path(env.get("NODEOPS_DB", "/opt/cloudbet/nodeops/nodeops.db"))
        self.sample_secs = int(env.get("NODEOPS_SAMPLE_SECS", "60"))
        self.retention_days = int(env.get("NODEOPS_RETENTION_DAYS", "30"))
        user = env.get("NODEOPS_USER") or None
        password = env.get("NODEOPS_PASSWORD") or None
        # Treat the shipped install placeholders as unset so a fresh install can never
        # come up authenticated with CHANGE_ME:CHANGE_ME.
        self.user = None if user == "CHANGE_ME" else user
        self.password = None if password == "CHANGE_ME" else password  # noqa: S105 - placeholder sentinel, not a credential
        self.readonly = _truthy(env.get("NODEOPS_READONLY", "1"))
        self.deploy_script = env.get(
            "NODEOPS_DEPLOY_SCRIPT",
            "scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh",
        )
        self.archive_script = env.get(
            "NODEOPS_ARCHIVE_SCRIPT",
            "scripts/deploy/strategy_nodes/archive_strategy_nodes.sh",
        )
        self.env_file = env.get("NODEOPS_ENV_FILE") or None
        self.repo_dir = Path(env.get("NODEOPS_REPO_DIR", os.getcwd()))
        self.static_dir = Path(__file__).resolve().parent

    @property
    def auth_enabled(self) -> bool:
        return self.user is not None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# An empty host binds all interfaces (INADDR_ANY), so it is deliberately NOT loopback.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _insecure_public_bind(config: Config) -> bool:
    """
    Return whether this is a public bind with auth disabled (the dangerous case).
    """
    return not config.auth_enabled and not _is_loopback(config.host)


def valid_name(name: str) -> bool:
    """
    Return whether ``name`` is a safe node/container identifier.
    """
    return bool(NAME_PATTERN.fullmatch(name or ""))


def safe_manifest_path(manifest_path: str) -> str | None:
    """
    Return a normalized manifest path if it is safe, else ``None``.

    The path must be relative, stay under ``deploy/strategy_nodes/`` after
    normalization (rejecting ``..`` traversal), and end in ``.json``.

    """
    if not manifest_path or manifest_path.startswith("/"):
        return None
    if not manifest_path.endswith(MANIFEST_SUFFIX):
        return None
    normalized = os.path.normpath(manifest_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        return None
    posix = Path(normalized).as_posix()
    if not posix.startswith(MANIFEST_PREFIX):
        return None
    return posix


def _flag_on(value: Any) -> bool:
    """
    Return the truthiness of a manifest flag that may be a JSON bool/int or string.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def manifest_is_validation_safe(manifest: Any) -> bool:
    """
    Return whether a manifest is data-only and safe for the dashboard to deploy.

    The dashboard's non-goal is that it must be structurally incapable of arming
    live execution, so a manifest qualifies only when ``validation_mode`` is set
    and every execution-arming switch is off — both the strategy-level flags and
    each venue's ``execution_enabled``. This rejects the committed ``*-live-pilot``
    and ``*-execution-readiness`` manifests, which arm real or dry-run execution.

    """
    if not isinstance(manifest, dict):
        return False
    if not _flag_on(manifest.get("validation_mode")):
        return False
    strategy = manifest.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    arming_flags = (
        "auto_execute",
        "live_execution_armed",
        "allow_same_venue_live_execution",
        "allow_cross_currency_live_execution",
        "value_execution_enabled",
    )
    if any(_flag_on(strategy.get(flag)) for flag in arming_flags):
        return False
    venues = manifest.get("venues")
    if isinstance(venues, list):
        return not any(
            isinstance(venue, dict) and _flag_on(venue.get("execution_enabled")) for venue in venues
        )
    return True


def strip_secrets(value: Any) -> Any:
    """
    Recursively drop mapping keys that look like credentials.

    Manifests are already secrets-free; this is defence in depth so a stray
    ``*_API_KEY``/``PASSWORD``/``TOKEN``/``SECRET`` value can never leave the host.

    """
    if isinstance(value, dict):
        return {
            key: strip_secrets(item)
            for key, item in value.items()
            if not SECRET_KEY_PATTERN.search(str(key))
        }
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Store:
    """
    SQLite-backed sample store (WAL mode, one connection per accessor).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    node TEXT NOT NULL,
                    container_state TEXT,
                    heartbeat_age_secs REAL,
                    image TEXT,
                    subscribed_instruments INTEGER,
                    graph_nodes INTEGER,
                    graph_edges INTEGER,
                    quoted_edges INTEGER,
                    semantic_match_instruments INTEGER,
                    cross_venue_candidate_count INTEGER,
                    rag_green INTEGER,
                    rag_amber INTEGER,
                    rag_red INTEGER,
                    raw_detections INTEGER,
                    valid_opportunities INTEGER,
                    executable_candidates INTEGER,
                    executed INTEGER,
                    mem_mb REAL,
                    cpu_pct REAL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_node_ts
                    ON samples (node, ts_utc);
                CREATE TABLE IF NOT EXISTS odds_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    node TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_odds_node_ts
                    ON odds_samples (node, ts_utc);
                """,
            )
            self._conn.commit()

    def insert_sample(self, row: dict[str, Any]) -> None:
        """
        Insert one ``samples`` row keyed by the fixed column order.
        """
        placeholders = ", ".join("?" for _ in SAMPLE_COLUMNS)
        columns = ", ".join(SAMPLE_COLUMNS)
        values = [row.get(column) for column in SAMPLE_COLUMNS]
        with self._lock:
            self._conn.execute(
                # columns/placeholders derive from the fixed SAMPLE_COLUMNS tuple; values bound.
                f"INSERT INTO samples ({columns}) VALUES ({placeholders})",  # noqa: S608
                values,
            )
            self._conn.commit()

    def insert_odds_sample(self, ts_utc: str, node: str, kind: str, payload: Any) -> None:
        """
        Persist an odds snapshot (``topPositiveCandidates`` etc.) as JSON.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO odds_samples (ts_utc, node, kind, payload_json) VALUES (?, ?, ?, ?)",
                (ts_utc, node, kind, json.dumps(payload, default=str)),
            )
            self._conn.commit()

    def latest_sample(self, node: str) -> dict[str, Any] | None:
        """
        Return the most recent sample row for ``node``.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM samples WHERE node = ? ORDER BY ts_utc DESC, id DESC LIMIT 1",
                (node,),
            )
            record = cursor.fetchone()
        return dict(record) if record is not None else None

    def latest_samples(self) -> dict[str, dict[str, Any]]:
        """
        Return the latest sample per node, keyed by node name.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT s.* FROM samples s
                JOIN (
                    SELECT node, MAX(id) AS max_id FROM samples GROUP BY node
                ) latest ON s.id = latest.max_id
                """,
            )
            records = cursor.fetchall()
        return {record["node"]: dict(record) for record in records}

    def history(self, node: str, hours: float, metrics: list[str]) -> dict[str, Any]:
        """
        Return time-series rows for ``node`` over the trailing ``hours``.

        ``metrics`` is filtered against the known numeric columns; unknown names
        are dropped. ``ts_utc`` is always included as the series index.

        """
        selected = [metric for metric in metrics if metric in HISTORY_METRICS]
        if not selected:
            selected = ["graph_edges", "quoted_edges", "cross_venue_candidate_count"]
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        columns = ", ".join(["ts_utc", *selected])
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {columns} FROM samples "  # noqa: S608 - columns are allow-listed
                "WHERE node = ? AND ts_utc >= ? ORDER BY ts_utc ASC",
                (node, cutoff),
            )
            rows = [dict(record) for record in cursor.fetchall()]
        return {"node": node, "hours": hours, "metrics": selected, "points": rows}

    def prune(self, retention_days: int) -> int:
        """
        Delete samples older than ``retention_days``; return rows removed.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        with self._lock:
            cursor = self._conn.execute("DELETE FROM samples WHERE ts_utc < ?", (cutoff,))
            self._conn.execute("DELETE FROM odds_samples WHERE ts_utc < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def read_json_file(path: Path) -> dict[str, Any] | None:
    """
    Load and parse a JSON file, returning ``None`` on any failure.

    A ``PermissionError`` is logged rather than silently swallowed: on the deploy
    host the node ``status.json``/``heartbeat.json`` files are root-owned, so an
    EACCES means the service is misconfigured (e.g. running under a non-root
    ``User=``) and every probe read would otherwise flatten to zero invisibly.

    """
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except PermissionError:
        logger.warning("permission denied reading %s; nodeops needs read access", path)
        return None
    except (OSError, ValueError):
        return None


def heartbeat_age_secs(heartbeat: dict[str, Any] | None, now: datetime) -> float | None:
    """
    Compute seconds since a heartbeat's ``at`` timestamp (``...Z`` UTC).
    """
    if not heartbeat:
        return None
    at = heartbeat.get("at")
    if not isinstance(at, str):
        return None
    try:
        stamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, (now - stamp).total_seconds())


def docker_inspect(container: str) -> dict[str, Any] | None:
    """
    Return ``{state, image}`` for a container via ``docker inspect``.
    """
    if not valid_name(container):
        return None
    result = _run_docker(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}\t{{.Config.Image}}",
            container,
        ],
    )
    if result is None:
        return None
    parts = result.strip().split("\t")
    state = parts[0] if parts else None
    image = parts[1] if len(parts) > 1 else None
    return {"state": state, "image": image}


def docker_stats(container: str) -> dict[str, Any]:
    """
    Return ``{mem_mb, cpu_pct}`` for a container via ``docker stats``.
    """
    if not valid_name(container):
        return {"mem_mb": None, "cpu_pct": None}
    result = _run_docker(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}\t{{.CPUPerc}}",
            container,
        ],
    )
    if result is None:
        return {"mem_mb": None, "cpu_pct": None}
    parts = result.strip().split("\t")
    mem_mb = _parse_mem_mb(parts[0]) if parts else None
    cpu_pct = _parse_percent(parts[1]) if len(parts) > 1 else None
    return {"mem_mb": mem_mb, "cpu_pct": cpu_pct}


def _parse_mem_mb(mem_usage: str) -> float | None:
    """
    Parse the ``used / limit`` MemUsage cell into MiB.
    """
    used = mem_usage.split("/")[0].strip()
    match = re.match(r"([0-9.]+)\s*([A-Za-z]+)", used)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024 * 1024),
        "kib": 1 / 1024,
        "mib": 1.0,
        "gib": 1024.0,
        "tib": 1024 * 1024,
    }
    return round(amount * factors.get(unit, 1.0), 3)


def _parse_percent(percent: str) -> float | None:
    try:
        return float(percent.strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _run_docker(args: list[str], timeout: float = 15.0) -> str | None:
    """
    Run a docker CLI command (argument list only) and return stdout.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - args are a fixed list, names allow-listed
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def build_sample_row(
    node: str,
    status: dict[str, Any] | None,
    heartbeat: dict[str, Any] | None,
    inspect: dict[str, Any] | None,
    stats: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """
    Flatten probe/heartbeat/docker data into a ``samples`` row.

    Reads the runtime-probe JSON paths produced by the betting-arbitrage runner
    (``runtimeProbe.venueCoverage.crossVenueCandidateCount``,
    ``runtimeProbe.candidateQuality.ragBands``, ``runtimeProbe.strategyStats``),
    falling back to zero when a key is absent.

    """
    probe = (status or {}).get("runtimeProbe") or {}
    venue_coverage = probe.get("venueCoverage") or {}
    candidate_quality = probe.get("candidateQuality") or {}
    rag_bands = candidate_quality.get("ragBands") or {}
    strategy_stats = probe.get("strategyStats") or {}
    inspect = inspect or {}
    return {
        "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node": node,
        "container_state": inspect.get("state"),
        "heartbeat_age_secs": heartbeat_age_secs(heartbeat, now),
        "image": inspect.get("image"),
        "subscribed_instruments": _as_int(probe.get("subscribedInstruments")),
        "graph_nodes": _as_int(probe.get("graphNodes")),
        "graph_edges": _as_int(probe.get("graphEdges")),
        "quoted_edges": _as_int(probe.get("quotedEdges")),
        "semantic_match_instruments": _as_int(probe.get("semanticMatchInstruments")),
        "cross_venue_candidate_count": _as_int(venue_coverage.get("crossVenueCandidateCount")),
        "rag_green": _as_int(rag_bands.get("green")),
        "rag_amber": _as_int(rag_bands.get("amber")),
        "rag_red": _as_int(rag_bands.get("red")),
        "raw_detections": _as_int(strategy_stats.get("raw_arbitrage_detections")),
        "valid_opportunities": _as_int(strategy_stats.get("opportunities_found")),
        "executable_candidates": _as_int(strategy_stats.get("executable_candidates")),
        "executed": _as_int(strategy_stats.get("opportunities_executed")),
        "mem_mb": stats.get("mem_mb"),
        "cpu_pct": stats.get("cpu_pct"),
    }


class Sampler(threading.Thread):
    """
    Background thread that samples every node into the store on an interval.
    """

    def __init__(self, config: Config, store: Store, stop_event: threading.Event) -> None:
        super().__init__(name="nodeops-sampler", daemon=True)
        self._config = config
        self._store = store
        self._stop_event = stop_event

    def run(self) -> None:
        # First sample immediately so the dashboard is populated on start-up.
        self.sample_once()
        while not self._stop_event.wait(self._config.sample_secs):
            self.sample_once()

    def sample_once(self) -> None:
        """
        Sample every node directory once, isolating per-node failures.
        """
        try:
            node_dirs = sorted(path for path in self._config.nodes_root.iterdir() if path.is_dir())
        except OSError as exc:
            logger.warning("nodes root %s not readable: %s", self._config.nodes_root, exc)
            return
        for node_dir in node_dirs:
            node = node_dir.name
            if node == "archives" or not valid_name(node):
                continue
            try:
                self._sample_node(node, node_dir)
            except Exception:
                # One bad node must never take down the sampler thread.
                logger.exception("sampling node %s failed", node)
        try:
            removed = self._store.prune(self._config.retention_days)
            if removed:
                logger.info("pruned %d expired sample rows", removed)
        except Exception:
            logger.exception("pruning samples failed")

    def _sample_node(self, node: str, node_dir: Path) -> None:
        now = datetime.now(UTC)
        status = read_json_file(node_dir / "status.json")
        heartbeat = read_json_file(node_dir / "heartbeat.json")
        inspect = docker_inspect(node)
        stats = docker_stats(node)
        row = build_sample_row(node, status, heartbeat, inspect, stats, now)
        self._store.insert_sample(row)
        self._record_odds_samples(node, status, row["ts_utc"])

    def _record_odds_samples(
        self,
        node: str,
        status: dict[str, Any] | None,
        ts_utc: str,
    ) -> None:
        candidate_quality = ((status or {}).get("runtimeProbe") or {}).get("candidateQuality") or {}
        for kind, key in (
            ("topPositiveCandidates", "topPositiveCandidates"),
            ("topNegativeNearMisses", "topNegativeNearMisses"),
        ):
            payload = candidate_quality.get(key)
            if payload:
                self._store.insert_odds_sample(ts_utc, node, kind, payload)


class Jobs:
    """
    In-memory registry of background deploy/archive jobs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, kind: str, target: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "target": target,
                "state": "running",
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        return job_id

    def finish(self, job_id: str, returncode: int, stdout: str, stderr: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["state"] = "succeeded" if returncode == 0 else "failed"
            job["returncode"] = returncode
            job["stdout"] = stdout[-8000:]
            job["stderr"] = stderr[-8000:]
            job["finished_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None


def run_job(config: Config, jobs: Jobs, job_id: str, args: list[str]) -> None:
    """
    Execute a deploy/archive script in a background thread, recording status.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - args are a fixed list, inputs validated
            args,
            cwd=str(config.repo_dir),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        jobs.finish(job_id, completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired:
        jobs.finish(job_id, 124, "", "job timed out")
    except (OSError, subprocess.SubprocessError) as exc:
        jobs.finish(job_id, 1, "", str(exc))


class NodeOpsState:
    """
    Bundle of long-lived collaborators shared by every request handler.
    """

    def __init__(self, config: Config, store: Store, jobs: Jobs) -> None:
        self.config = config
        self.store = store
        self.jobs = jobs


class Handler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the JSON API and the static frontend.
    """

    server_version = "nodeops/1.0"

    @property
    def state(self) -> NodeOpsState:
        # NodeOpsState is attached to the server in build_server; BaseHTTPRequestHandler
        # exposes it via self.server, so resolve it there rather than expecting an
        # instance attribute (there is none — every request would 500 otherwise).
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_auth_challenge(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="nodeops"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        config = self.state.config
        if not config.auth_enabled:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        user_ok = hmac.compare_digest(user, config.user or "")
        password_ok = hmac.compare_digest(password, config.password or "")
        return user_ok and password_ok

    def _read_body(self) -> dict[str, Any]:
        length = _as_int(self.headers.get("Content-Length"))
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _readonly_blocked(self) -> bool:
        if self.state.config.readonly:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "read-only mode"})
            return True
        return False

    # -- dispatch ---------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._authorized():
            self._send_auth_challenge()
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            self._route(method, path, query)
        except BrokenPipeError:
            raise
        except Exception:
            logger.exception("handler error for %s %s", method, path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        if method == "GET" and path in {"/", "/index.html"}:
            self._serve_index()
            return
        if method == "GET" and path == "/api/nodes":
            self._list_nodes()
            return
        if method == "POST" and path == "/api/nodes":
            self._deploy_node()
            return

        job_match = re.fullmatch(r"/api/jobs/([0-9a-fA-F]+)", path)
        if method == "GET" and job_match:
            self._get_job(job_match.group(1))
            return

        node_match = re.fullmatch(r"/api/nodes/([^/]+)(/history|/restart|/stop|/start)?", path)
        if node_match:
            name = node_match.group(1)
            action = node_match.group(2)
            self._route_node(method, name, action, query)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _route_node(
        self,
        method: str,
        name: str,
        action: str | None,
        query: dict[str, list[str]],
    ) -> None:
        if not valid_name(name):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid node name"})
            return
        if method == "GET" and action == "/history":
            self._node_history(name, query)
            return
        if method == "GET" and action is None:
            self._node_detail(name)
            return
        if method == "POST" and action in {"/restart", "/stop", "/start"}:
            self._node_lifecycle(name, action.lstrip("/"))
            return
        if method == "DELETE" and action is None:
            self._delete_node(name)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # -- read routes ------------------------------------------------------------

    def _serve_index(self) -> None:
        index_path = self.state.config.static_dir / "index.html"
        try:
            body = index_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html missing"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _list_nodes(self) -> None:
        config = self.state.config
        samples = self.state.store.latest_samples()
        try:
            dir_names = {
                path.name
                for path in config.nodes_root.iterdir()
                if path.is_dir() and path.name != "archives" and valid_name(path.name)
            }
        except OSError:
            dir_names = set()
        names = sorted(dir_names | set(samples))
        nodes = [{"node": name, "latest": samples.get(name)} for name in names]
        self._send_json(
            HTTPStatus.OK,
            {"readonly": config.readonly, "nodes": nodes},
        )

    def _node_detail(self, name: str) -> None:
        config = self.state.config
        node_dir = config.nodes_root / name
        status = read_json_file(node_dir / "status.json")
        manifest = read_json_file(node_dir / "manifest.runtime.json")
        latest = self.state.store.latest_sample(name)
        payload = {
            "node": name,
            "readonly": config.readonly,
            "latest": latest,
            "manifest": strip_secrets(manifest) if manifest is not None else None,
            "runtimeProbe": strip_secrets((status or {}).get("runtimeProbe")),
            "status": strip_secrets(_status_summary(status)),
            "containerState": (latest or {}).get("container_state"),
            "image": (latest or {}).get("image"),
        }
        self._send_json(HTTPStatus.OK, payload)

    def _node_history(self, name: str, query: dict[str, list[str]]) -> None:
        hours = _as_float(query.get("hours", ["24"])[0]) or 24.0
        metrics_arg = query.get("metrics", [""])[0]
        metrics = [metric for metric in metrics_arg.split(",") if metric]
        history = self.state.store.history(name, hours, metrics)
        self._send_json(HTTPStatus.OK, history)

    def _get_job(self, job_id: str) -> None:
        job = self.state.jobs.get(job_id)
        if job is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
            return
        self._send_json(HTTPStatus.OK, job)

    # -- mutating routes --------------------------------------------------------

    def _deploy_node(self) -> None:
        if self._readonly_blocked():
            return
        config = self.state.config
        body = self._read_body()
        container = str(body.get("container_name") or body.get("name") or "")
        image = str(body.get("image") or "")
        manifest_path = str(body.get("manifest_path") or "")
        if not valid_name(container):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid container_name"})
            return
        if not image or not valid_image(image):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid image"})
            return
        safe_manifest = safe_manifest_path(manifest_path)
        if safe_manifest is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid manifest_path"})
            return
        manifest = read_json_file(config.repo_dir / safe_manifest)
        if manifest is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "manifest not found or unreadable"},
            )
            return
        if not manifest_is_validation_safe(manifest):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "manifest is not validation-safe; nodeops only deploys "
                    "data-only nodes (validation_mode with no execution armed)",
                },
            )
            return
        args = [
            "bash",
            config.deploy_script,
            "--manifest",
            safe_manifest,
            "--image",
            image,
            "--name",
            container,
            "--root",
            str(config.nodes_root),
        ]
        if config.env_file:
            args += ["--env-file", config.env_file]
        self._start_job("deploy", container, args)

    def _node_lifecycle(self, name: str, action: str) -> None:
        if self._readonly_blocked():
            return
        # ``--`` terminates flag parsing so the name can never be read as an option.
        result = _run_docker(["docker", action, "--", name])
        if result is None:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"docker {action} failed"})
            return
        self._send_json(HTTPStatus.OK, {"node": name, "action": action, "ok": True})

    def _delete_node(self, name: str) -> None:
        if self._readonly_blocked():
            return
        config = self.state.config
        args = [
            "bash",
            config.archive_script,
            "--container",
            name,
            "--root",
            str(config.nodes_root),
            "--remove",
        ]
        self._start_job("archive", name, args)

    def _start_job(self, kind: str, target: str, args: list[str]) -> None:
        job_id = self.state.jobs.create(kind, target)
        thread = threading.Thread(
            target=run_job,
            args=(self.state.config, self.state.jobs, job_id, args),
            name=f"nodeops-job-{job_id}",
            daemon=True,
        )
        thread.start()
        self._send_json(HTTPStatus.ACCEPTED, {"job_id": job_id, "kind": kind, "target": target})


def valid_image(image: str) -> bool:
    """
    Return whether an image ref is a safe docker reference string.
    """
    return bool(IMAGE_PATTERN.fullmatch(image or ""))


def _status_summary(status: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Return the non-probe status fields (state, timestamps) for the drawer.
    """
    if not status:
        return None
    return {key: value for key, value in status.items() if key != "runtimeProbe"}


def build_server(config: Config, store: Store, jobs: Jobs) -> ThreadingHTTPServer:
    """
    Construct the threading HTTP server with shared state attached.
    """
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.state = NodeOpsState(config, store, jobs)  # type: ignore[attr-defined]
    return server


def main() -> int:
    """Entry point: start the sampler and serve until SIGTERM/SIGINT."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config()
    if _insecure_public_bind(config):
        logger.error(
            "refusing to start: NODEOPS_HOST=%s is not loopback but HTTP Basic auth is "
            "not configured. Set NODEOPS_USER/NODEOPS_PASSWORD, or bind 127.0.0.1.",
            config.host,
        )
        return 2
    if not config.auth_enabled:
        logger.warning(
            "HTTP Basic auth is DISABLED (loopback bind only). Set NODEOPS_USER/"
            "NODEOPS_PASSWORD before exposing this service beyond localhost.",
        )
    store = Store(config.db_path)
    jobs = Jobs()
    stop_event = threading.Event()
    sampler = Sampler(config, store, stop_event)
    sampler.start()
    server = build_server(config, store, jobs)

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("received signal %s; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "nodeops serving on http://%s:%d (readonly=%s, nodes_root=%s)",
        config.host,
        config.port,
        config.readonly,
        config.nodes_root,
    )
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        store.close()
        logger.info("nodeops stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
