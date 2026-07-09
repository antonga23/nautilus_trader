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
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
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

# Bounded in-memory alert ring returned by GET /api/alerts, and the POST timeout for
# the outbound webhook (short — a slow/unreachable webhook must never stall a sample).
ALERT_RING_SIZE = 200
ALERT_WEBHOOK_TIMEOUT = 5.0
# The most recent N audit rows GET /api/audit returns when no limit is supplied.
AUDIT_DEFAULT_LIMIT = 100
AUDIT_MAX_LIMIT = 1000
# Cap request bodies: deploy/lifecycle payloads are tiny JSON, so refuse anything
# larger rather than buffering an attacker-supplied Content-Length into memory.
MAX_BODY_BYTES = 1 << 20

# Persisted-credential store parameters. pbkdf2-hmac-sha256 with a per-record random
# salt; the password is never written to disk in the clear. 200k iterations is the
# spec floor — a stored record may carry a higher count and still verify.
AUTH_ITERATIONS = 200000
AUTH_ALGO = "pbkdf2_hmac_sha256"
AUTH_VERSION = 1
# New-password bounds enforced by POST /api/auth/change.
AUTH_MIN_PASSWORD_LEN = 8
AUTH_MAX_PASSWORD_LEN = 256
AUTH_MAX_USERNAME_LEN = 64

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
    "started_at",
    "uptime_secs",
)
# Numeric metric columns the history endpoint may chart.
HISTORY_METRICS = frozenset(
    {
        "heartbeat_age_secs",
        "uptime_secs",
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

# Columns added after the first deployed DB was created; Store adds them via
# additive ALTER TABLE on startup so an existing samples table upgrades in place.
SAMPLES_MIGRATED_COLUMNS = (
    ("started_at", "TEXT"),
    ("uptime_secs", "REAL"),
)

# POST /api/nodes/<name>/config may rewrite ONLY these manifest fields (plus the
# ``restart`` action flag). They widen/narrow the node's instrument-discovery
# window and can never arm execution; everything else is rejected outright.
CONFIG_ALLOWED_KEYS = frozenset(
    {
        "max_resolution_horizon_hours",
        "market_timing_filter",
        "instrument_refresh_interval_secs",
        "restart",
    },
)
MARKET_TIMING_FILTERS = frozenset({"all", "pre_market"})
MAX_HORIZON_HOURS = 720.0
MIN_REFRESH_SECS = 30.0


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
        # The credential store lives beside the DB (``.../nodeops/auth.json`` by
        # default). NODEOPS_AUTH_FILE overrides the location verbatim for tests.
        auth_file = env.get("NODEOPS_AUTH_FILE")
        self.auth_path = Path(auth_file) if auth_file else self.db_path.parent / "auth.json"
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
        # Optional push-alerting webhook. When set, the sampler POSTs a JSON alert on
        # each detected per-node state transition (quoting on/off, heartbeat stale,
        # container left running, cross-venue arb found). Unset ⇒ alerting is inert.
        self.alert_webhook = env.get("NODEOPS_ALERT_WEBHOOK") or None
        self.heartbeat_stale_secs = float(env.get("NODEOPS_HEARTBEAT_STALE_SECS", "180"))

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


def _positive_number(value: Any) -> float | None:
    """
    Return ``value`` as a finite positive float, or ``None`` when it is not one.

    Booleans are rejected explicitly (they are ``int`` subclasses and would
    otherwise coerce to 0.0/1.0).

    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def validate_config_body(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """
    Validate a ``POST /api/nodes/<name>/config`` request body.

    Returns ``(updates, "")`` with the allow-listed manifest field updates on
    success, or ``(None, message)`` for the first rejection. ``restart`` is
    validated here but is an action flag, not a manifest field, so it is never
    included in ``updates``.

    """
    unknown = sorted(set(body) - CONFIG_ALLOWED_KEYS)
    if unknown:
        return None, "unknown keys: " + ", ".join(unknown)
    updates: dict[str, Any] = {}
    if "max_resolution_horizon_hours" in body:
        horizon = _positive_number(body["max_resolution_horizon_hours"])
        if horizon is None or horizon > MAX_HORIZON_HOURS:
            return (
                None,
                f"max_resolution_horizon_hours must be a number in (0, {MAX_HORIZON_HOURS:g}]",
            )
        updates["max_resolution_horizon_hours"] = horizon
    if "market_timing_filter" in body:
        timing = body["market_timing_filter"]
        if timing not in MARKET_TIMING_FILTERS:
            return None, "market_timing_filter must be one of: all, pre_market"
        updates["market_timing_filter"] = timing
    if "instrument_refresh_interval_secs" in body:
        refresh = _positive_number(body["instrument_refresh_interval_secs"])
        if refresh is None or refresh < MIN_REFRESH_SECS:
            return (
                None,
                f"instrument_refresh_interval_secs must be a number >= {MIN_REFRESH_SECS:g}",
            )
        updates["instrument_refresh_interval_secs"] = refresh
    if "restart" in body and not isinstance(body["restart"], bool):
        return None, "restart must be a boolean"
    if not updates:
        return None, "no config fields provided"
    return updates, ""


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


def _utc_now_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- RAG derivation -------------------------------------------------------------


def _band_is_negative(label: str) -> bool:
    """
    Return whether a ``marginBands`` bucket label denotes a negative margin.

    ``"positive"`` is the aggregate profitable bucket and is never negative. Any
    other label containing a minus sign is a loss bucket — e.g. ``"< -5%"``,
    ``"-5% to -1%"``, and the near-loss ``"0% to -1%"`` all count toward RED.

    """
    if label.strip().lower() == "positive":
        return False
    return "-" in label


def _derive_rag(probe: dict[str, Any]) -> tuple[int, int, int]:
    """
    Derive ``(green, amber, red)`` counts from live runtime-probe fields.

    The node never populates ``candidateQuality.ragBands`` (a dead field), so the
    dashboard RAG is derived deterministically instead:

    - GREEN = profitable *and* execution-eligible edges. The profitable count (the
      ``"positive"`` aggregate bucket, or the sum of non-negative buckets when it is
      absent) is capped by ``executionSafeEdges`` and by ``executable_candidates``
      when those are present, and forced to 0 when nothing is quoted (an unquoted
      node cannot hold a live tradeable edge).
    - RED = the summed count of every negative-margin ``marginBands`` bucket.
    - AMBER = the profitable-but-not-green remainder (blocked / near-miss / stale),
      never negative.

    """
    candidate_quality = probe.get("candidateQuality") or {}
    strategy_stats = probe.get("strategyStats") or {}
    margin_bands = candidate_quality.get("marginBands") or {}
    quoted = _as_int(probe.get("quotedEdges"))

    if "positive" in margin_bands:
        positive = _as_int(margin_bands.get("positive"))
    else:
        positive = sum(
            _as_int(count) for label, count in margin_bands.items() if not _band_is_negative(label)
        )

    red = sum(_as_int(count) for label, count in margin_bands.items() if _band_is_negative(label))

    caps = [positive]
    safe = candidate_quality.get("executionSafeEdges")
    if safe is not None:
        caps.append(_as_int(safe))
    exec_cand = _as_int(strategy_stats.get("executable_candidates"))
    if exec_cand > 0:
        caps.append(exec_cand)
    green = 0 if quoted == 0 else max(0, min(caps))

    amber = max(0, positive - green)
    return green, amber, red


# -- credential store -----------------------------------------------------------


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf8"), salt, iterations)


def _make_credential(username: str, password: str, *, is_default: bool) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    derived = _hash_password(password, salt, AUTH_ITERATIONS)
    return {
        "version": AUTH_VERSION,
        "username": username,
        "algo": AUTH_ALGO,
        "iterations": AUTH_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(derived).decode("ascii"),
        "is_default": is_default,
        "updated_at": _utc_now_str(),
    }


def _verify_password(record: dict[str, Any], username: str, password: str) -> bool:
    """
    Constant-time verify ``username``/``password`` against a stored credential.

    The pbkdf2 derivation always runs before returning even when the username
    mismatches, so a wrong username costs the same as a wrong password and cannot be
    used to enumerate valid accounts by timing.

    """
    if record.get("algo") != AUTH_ALGO:
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError):
        return False
    derived = _hash_password(password, salt, iterations)
    user_ok = hmac.compare_digest(str(record.get("username", "")), username)
    pass_ok = hmac.compare_digest(expected, derived)
    return user_ok and pass_ok


def _valid_username(username: str) -> bool:
    """
    Return whether a username is acceptable for the credential store.

    Usernames are ``1..64`` printable characters with no ``:`` (the Basic-auth
    field delimiter) and no control characters.

    """
    if not username or len(username) > AUTH_MAX_USERNAME_LEN:
        return False
    if ":" in username:
        return False
    return all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in username)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """
    Write ``data`` as JSON to ``path`` atomically with mode ``0600``.

    The payload is written to a temp file in the same directory (so ``os.replace``
    is an atomic same-filesystem rename), fsynced, then renamed over the target.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auth.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_manifest(path: Path, data: dict[str, Any]) -> None:
    """
    Atomically replace a node manifest with ``data`` as pretty-printed JSON.

    Same tmp-file + fsync + rename pattern as ``_atomic_write_json``, but the
    original file mode is preserved (the container must still be able to read
    the manifest, so it must not be tightened to 0600).

    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class AuthStore:
    """
    Persisted single-identity credential store backed by ``auth.json``.

    Holds one salted-hash admin record. Seeds ``admin``/``admin`` (hashed,
    ``is_default=true``) on first startup, verifies HTTP Basic against the stored
    hash in constant time, and rotates credentials atomically via ``change``.

    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        """
        Read and parse ``auth.json``; return ``None`` on missing/corrupt/unusable.
        """
        try:
            raw = self._path.read_text(encoding="utf8")
        except (OSError, ValueError):
            logger.debug("auth store %s absent or unreadable", self._path)
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            logger.warning("auth store %s is corrupt JSON; treating as absent", self._path)
            return None
        if not isinstance(record, dict):
            logger.warning("auth store %s is not an object; treating as absent", self._path)
            return None
        if _as_int(record.get("version")) > AUTH_VERSION:
            logger.warning("auth store %s has unknown version; treating as absent", self._path)
            return None
        if record.get("algo") != AUTH_ALGO:
            logger.warning("auth store %s has unknown algo; treating as absent", self._path)
            return None
        return record

    def seed_default_if_absent(self) -> None:
        """
        Write the hashed ``admin``/``admin`` default record when none exists.
        """
        with self._lock:
            if self.load() is not None:
                return
            _atomic_write_json(
                self._path,
                _make_credential("admin", "admin", is_default=True),
            )
            logger.info(
                "seeded default nodeops credential (admin/admin); change it on first login",
            )

    def verify(self, username: str, password: str) -> bool:
        record = self.load()
        return record is not None and _verify_password(record, username, password)

    def whoami(self) -> dict[str, Any]:
        record = self.load()
        if record is None:
            return {"username": None, "is_default": False}
        return {
            "username": record.get("username"),
            "is_default": bool(record.get("is_default")),
        }

    def change(
        self,
        current_password: str,
        new_username: str | None,
        new_password: str,
    ) -> tuple[bool, str]:
        """
        Rotate the stored credential after verifying ``current_password``.

        The current username is taken from the stored record (the change form only
        sends the current password). On success a new record is written with
        ``is_default=false`` and the new password, keeping the existing username
        when ``new_username`` is empty/absent.

        """
        with self._lock:
            record = self.load()
            if record is None:
                return False, "auth is disabled"
            existing_username = str(record.get("username", ""))
            if not _verify_password(record, existing_username, current_password):
                return False, "current password incorrect"
            username = new_username or existing_username
            _atomic_write_json(
                self._path,
                _make_credential(username, new_password, is_default=False),
            )
            return True, ""


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
        self._migrate_schema()

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
                    cpu_pct REAL,
                    started_at TEXT,
                    uptime_secs REAL
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    username TEXT,
                    action TEXT NOT NULL,
                    node TEXT,
                    params_summary TEXT,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts
                    ON audit_log (ts_utc);
                """,
            )
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """
        Add ``samples`` columns introduced after a deployed DB was created.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a DB
        created by an older build lacks the newer columns; add them additively
        (``ALTER TABLE ... ADD COLUMN``) when missing.

        """
        with self._lock:
            existing = {
                record["name"]
                for record in self._conn.execute("PRAGMA table_info(samples)").fetchall()
            }
            for column, ddl_type in SAMPLES_MIGRATED_COLUMNS:
                if column not in existing:
                    # column/type come from the fixed SAMPLES_MIGRATED_COLUMNS tuple.
                    self._conn.execute(f"ALTER TABLE samples ADD COLUMN {column} {ddl_type}")
                    logger.info("migrated samples table: added column %s %s", column, ddl_type)
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

    def latest_odds(self, node: str) -> dict[str, dict[str, Any]]:
        """
        Return the most recent odds row per ``kind`` for ``node``.

        Keyed by kind (``topPositiveCandidates`` etc.); each value carries the row
        ``ts_utc`` and the parsed ``payload_json``. Empty when the node has no odds.

        """
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT o.kind, o.ts_utc, o.payload_json FROM odds_samples o
                JOIN (
                    SELECT kind, MAX(id) AS max_id FROM odds_samples
                    WHERE node = ? GROUP BY kind
                ) latest ON o.id = latest.max_id
                """,
                (node,),
            )
            records = cursor.fetchall()
        result: dict[str, dict[str, Any]] = {}
        for record in records:
            try:
                payload = json.loads(record["payload_json"])
            except ValueError:
                payload = []
            result[record["kind"]] = {"ts_utc": record["ts_utc"], "payload": payload}
        return result

    def odds_stats(self, node: str) -> dict[str, Any]:
        """
        Return stored-odds count and most-recent timestamp for ``node``.

        Surfaces devig-panel context: how many ``odds_samples`` rows the sampler
        has persisted for the node and when the newest one landed. ``count`` is 0
        and ``latest_ts`` is ``None`` when the node has no stored odds.

        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) AS count, MAX(ts_utc) AS latest_ts "
                "FROM odds_samples WHERE node = ?",
                (node,),
            )
            record = cursor.fetchone()
        return {
            "count": _as_int(record["count"]) if record is not None else 0,
            "latest_ts": record["latest_ts"] if record is not None else None,
        }

    def insert_audit(
        self,
        ts_utc: str,
        username: str | None,
        action: str,
        node: str | None,
        params_summary: str,
        status: str,
    ) -> None:
        """
        Append one row to the ``audit_log`` for a mutating control action.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log "
                "(ts_utc, username, action, node, params_summary, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts_utc, username, action, node, params_summary, status),
            )
            self._conn.commit()

    def recent_audit(self, limit: int) -> list[dict[str, Any]]:
        """
        Return the most recent ``limit`` audit rows, newest first.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT ts_utc, username, action, node, params_summary, status "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(record) for record in cursor.fetchall()]

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


def parse_docker_timestamp(value: Any) -> datetime | None:
    """
    Parse docker's ``State.StartedAt`` timestamp into an aware UTC datetime.

    Docker emits RFC 3339 with nanosecond fractions (e.g.
    ``2026-07-04T17:46:05.123456789Z``), which Python 3.10's ``fromisoformat``
    cannot read, so the fraction is trimmed to microseconds and the trailing
    ``Z`` mapped to ``+00:00`` first. Docker's zero time (``0001-01-01...``,
    a never-started container) and unparsable values return ``None``.

    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def docker_inspect(container: str) -> dict[str, Any] | None:
    """
    Return ``{state, image, started_at}`` for a container via ``docker inspect``.

    ``started_at`` is the ``State.StartedAt`` timestamp normalized to the
    sampler's ``YYYY-MM-DDTHH:MM:SSZ`` format, or ``None`` when absent.

    """
    if not valid_name(container):
        return None
    result = _run_docker(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}\t{{.Config.Image}}\t{{.State.StartedAt}}",
            container,
        ],
    )
    if result is None:
        return None
    parts = result.strip().split("\t")
    state = parts[0] if parts else None
    image = parts[1] if len(parts) > 1 else None
    started = parse_docker_timestamp(parts[2]) if len(parts) > 2 else None
    started_at = started.strftime("%Y-%m-%dT%H:%M:%SZ") if started is not None else None
    return {"state": state, "image": image, "started_at": started_at}


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
    ``runtimeProbe.candidateQuality.marginBands``, ``runtimeProbe.strategyStats``),
    falling back to zero when a key is absent. RAG is derived from live probe
    fields (``ragBands`` is never populated by the node) via ``_derive_rag``.

    """
    probe = (status or {}).get("runtimeProbe") or {}
    venue_coverage = probe.get("venueCoverage") or {}
    strategy_stats = probe.get("strategyStats") or {}
    rag_green, rag_amber, rag_red = _derive_rag(probe)
    inspect = inspect or {}
    started_at = inspect.get("started_at")
    uptime_secs: float | None = None
    if inspect.get("state") == "running":
        started = parse_docker_timestamp(started_at)
        if started is not None:
            uptime_secs = max(0.0, (now - started).total_seconds())
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
        "rag_green": rag_green,
        "rag_amber": rag_amber,
        "rag_red": rag_red,
        "raw_detections": _as_int(strategy_stats.get("raw_arbitrage_detections")),
        "valid_opportunities": _as_int(strategy_stats.get("opportunities_found")),
        "executable_candidates": _as_int(strategy_stats.get("executable_candidates")),
        "executed": _as_int(strategy_stats.get("opportunities_executed")),
        "mem_mb": stats.get("mem_mb"),
        "cpu_pct": stats.get("cpu_pct"),
        "started_at": started_at,
        "uptime_secs": uptime_secs,
    }


# -- alerting -------------------------------------------------------------------


def _alert_node_state(row: dict[str, Any], stale_secs: float) -> dict[str, Any]:
    """
    Reduce a sample row to the coarse state the alert engine tracks per node.

    The tracked axes are the ones alert conditions transition on: whether the node
    is quoting, whether its heartbeat is stale, its container state, and whether it
    currently holds a cross-venue arb candidate. Kept deliberately small so a
    transition compare is a plain dict-field comparison.

    """
    hb_age = _as_float(row.get("heartbeat_age_secs"))
    return {
        "quoting": _as_int(row.get("quoted_edges")) > 0,
        "heartbeat_stale": hb_age is not None and hb_age > stale_secs,
        "container_state": row.get("container_state"),
        "cross_venue": _as_int(row.get("cross_venue_candidate_count")) > 0,
    }


def detect_transitions(
    prev: dict[str, Any] | None,
    curr: dict[str, Any],
) -> list[dict[str, str]]:
    """
    Return the alert events fired by the move from ``prev`` to ``curr`` state.

    ``prev is None`` is a node's first observed sample: there is no prior state to
    transition from, so nothing fires (the baseline is recorded, not alerted). Each
    event is ``{"condition", "severity", "detail"}``; the cross-venue-arb-found
    transition is flagged high severity.

    """
    if prev is None:
        return []
    events: list[dict[str, str]] = []
    if prev.get("quoting") and not curr.get("quoting"):
        events.append(
            {"condition": "quoting_stopped", "severity": "warning", "detail": "quoted edges → 0"},
        )
    elif not prev.get("quoting") and curr.get("quoting"):
        events.append(
            {"condition": "quoting_started", "severity": "info", "detail": "0 → quoted edges"},
        )
    if not prev.get("heartbeat_stale") and curr.get("heartbeat_stale"):
        events.append(
            {
                "condition": "heartbeat_stale",
                "severity": "warning",
                "detail": "heartbeat went stale",
            },
        )
    prev_state = prev.get("container_state")
    curr_state = curr.get("container_state")
    if prev_state == "running" and curr_state != "running":
        events.append(
            {
                "condition": "container_left_running",
                "severity": "warning",
                "detail": f"{prev_state} → {curr_state}",
            },
        )
    if not prev.get("cross_venue") and curr.get("cross_venue"):
        events.append(
            {
                "condition": "cross_venue_candidate",
                "severity": "high",
                "detail": "ARB FOUND — cross-venue candidate count → >0",
            },
        )
    return events


def post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """
    POST ``payload`` as JSON to ``url``; swallow and log any failure.

    Uses a short timeout so a slow or unreachable webhook can never stall the sampler
    loop. Returns whether the POST was accepted (2xx), for the test endpoint to report
    wiring status.

    """
    body = json.dumps(payload, default=str).encode("utf8")
    request = urllib.request.Request(  # noqa: S310 - operator-configured webhook URL
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - operator-configured webhook URL
            request,
            timeout=ALERT_WEBHOOK_TIMEOUT,
        ) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("alert webhook POST to %s failed: %s", url, exc)
        return False


class AlertState:
    """
    In-memory alert engine: per-node last state plus a bounded event ring.

    The sampler is a single process, so keeping last-alerted state in memory (rather
    than a table) is sufficient for de-dupe: an alert fires only on a state
    *transition*, never while a condition merely persists across samples.

    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._last_state: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    def _record(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if len(self._events) > ALERT_RING_SIZE:
            del self._events[: len(self._events) - ALERT_RING_SIZE]

    def observe(self, node: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Fold one sample row into the engine, firing alerts on any transition.

        Records the new per-node baseline, appends each fired event to the ring, and
        POSTs it to the configured webhook (if any). Returns the events fired so the
        caller can log them; de-dupe is inherent — unchanged state fires nothing.

        """
        curr = _alert_node_state(row, self._config.heartbeat_stale_secs)
        with self._lock:
            prev = self._last_state.get(node)
            self._last_state[node] = curr
            transitions = detect_transitions(prev, curr)
            fired: list[dict[str, Any]] = []
            for transition in transitions:
                event = {
                    "ts_utc": _utc_now_str(),
                    "node": node,
                    **transition,
                }
                self._record(event)
                fired.append(event)
        for event in fired:
            self._maybe_post(event)
        return fired

    def _maybe_post(self, event: dict[str, Any]) -> None:
        if self._config.alert_webhook:
            post_webhook(self._config.alert_webhook, event)

    def fire_synthetic(self) -> dict[str, Any]:
        """
        Emit a synthetic test alert (records it and POSTs it) to verify wiring.
        """
        event = {
            "ts_utc": _utc_now_str(),
            "node": "__test__",
            "condition": "test",
            "severity": "info",
            "detail": "synthetic test alert from POST /api/alerts/test",
        }
        with self._lock:
            self._record(event)
        posted = self._config.alert_webhook is not None
        if posted:
            post_webhook(self._config.alert_webhook or "", event)
        return {"event": event, "webhook_configured": posted}

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events[-limit:][::-1])


class Sampler(threading.Thread):
    """
    Background thread that samples every node into the store on an interval.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        stop_event: threading.Event,
        alerts: AlertState | None = None,
    ) -> None:
        super().__init__(name="nodeops-sampler", daemon=True)
        self._config = config
        self._store = store
        self._stop_event = stop_event
        self._alerts = alerts

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
        if self._alerts is not None:
            fired = self._alerts.observe(node, row)
            for event in fired:
                logger.info(
                    "alert [%s/%s] node=%s: %s",
                    event["severity"],
                    event["condition"],
                    node,
                    event["detail"],
                )

    def _record_odds_samples(
        self,
        node: str,
        status: dict[str, Any] | None,
        ts_utc: str,
    ) -> None:
        candidate_quality = ((status or {}).get("runtimeProbe") or {}).get("candidateQuality") or {}
        for kind in (
            "topPositiveCandidates",
            "topNegativeNearMisses",
            "topValueEdgeCandidates",
        ):
            payload = candidate_quality.get(kind)
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

    def __init__(
        self,
        config: Config,
        store: Store,
        jobs: Jobs,
        auth: AuthStore | None = None,
        alerts: AlertState | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.jobs = jobs
        # ``auth is None`` means the credential store is disabled: env-override mode
        # (auth handled by config.user/password) or the deliberate test seam where
        # _authorized's store branch returns True. main() always passes a real store
        # in production (non-env mode), so production is never unauthenticated.
        self.auth = auth
        # Shared alert engine; the sampler and the /api/alerts* routes both use it.
        # main() always passes one; fake-handler tests that omit it get None and the
        # alert routes degrade to an empty ring (never crash).
        self.alerts = alerts if alerts is not None else AlertState(config)


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

    def _auth_mode(self) -> str:
        """
        Return the effective auth mode: ``"env"``, ``"store"``, or ``"disabled"``.
        """
        if self.state.config.auth_enabled:
            return "env"
        if self.state.auth is not None:
            return "store"
        return "disabled"

    def _authorized(self) -> bool:
        config = self.state.config
        if config.auth_enabled:
            # env-override path — plaintext compare_digest, unchanged from the
            # pre-store implementation so every existing env-auth test still holds.
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
        # store path
        auth = self.state.auth
        if auth is None:
            # No env auth and no store ⇒ auth disabled (the test seam / loopback dev
            # with the store unavailable). Production always attaches a real store.
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        return auth.verify(user, password)

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

    def _current_username(self) -> str | None:
        """
        Return the authenticated identity for audit rows.

        In env-override mode the identity is ``config.user``; otherwise it is the
        username carried in the Basic header (already verified by ``_authorized``
        before dispatch). Auth-disabled requests have no identity.

        """
        config = self.state.config
        if config.auth_enabled:
            return config.user
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, _, _ = decoded.partition(":")
        return user or None

    def _audit(
        self,
        action: str,
        node: str | None,
        params: dict[str, Any] | None,
        status: str,
    ) -> None:
        """
        Record one audit row for a mutating control.

        ``params`` is run through ``strip_secrets`` and compact-JSON-encoded so no
        credential value can ever land in the log. Auditing must never break the
        request path, so any store error is swallowed and logged.

        """
        store = self.state.store
        if not hasattr(store, "insert_audit"):
            return
        summary = json.dumps(strip_secrets(params or {}), default=str, sort_keys=True)
        try:
            store.insert_audit(
                _utc_now_str(),
                self._current_username(),
                action,
                node,
                summary,
                status,
            )
        except Exception:
            logger.exception("writing audit row for %s failed", action)

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
        # Fixed (method, path) routes dispatched by table so this stays flat as the
        # endpoint list grows. Query-taking handlers are wrapped to a nullary shape.
        fixed: dict[tuple[str, str], Any] = {
            ("GET", "/api/nodes"): self._list_nodes,
            ("POST", "/api/nodes"): self._deploy_node,
            ("GET", "/api/auth/whoami"): self._auth_whoami,
            ("POST", "/api/auth/change"): self._auth_change,
            ("GET", "/api/alerts"): self._list_alerts,
            ("POST", "/api/alerts/test"): self._alerts_test,
            ("GET", "/api/audit"): lambda: self._list_audit(query),
        }
        handler = fixed.get((method, path))
        if handler is not None:
            handler()
            return

        job_match = re.fullmatch(r"/api/jobs/([0-9a-fA-F]+)", path)
        if method == "GET" and job_match:
            self._get_job(job_match.group(1))
            return

        node_match = re.fullmatch(
            r"/api/nodes/([^/]+)(/history|/odds|/restart|/stop|/start|/config)?",
            path,
        )
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
        if method == "GET" and action == "/odds":
            self._node_odds(name)
            return
        if method == "GET" and action is None:
            self._node_detail(name)
            return
        if method == "POST" and action in {"/restart", "/stop", "/start"}:
            self._node_lifecycle(name, action.lstrip("/"))
            return
        if method == "POST" and action == "/config":
            self._node_config(name)
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
        release = read_json_file(node_dir / "release.json")
        latest = self.state.store.latest_sample(name)
        probe = strip_secrets((status or {}).get("runtimeProbe"))
        # Devig-diagnostics panel: surface the probe's candidateQuality.devigDiagnostics
        # (overround / vig / value-edge percentiles / method counts) alongside how many
        # odds_samples rows the sampler has stored for this node and when. Read-only
        # visibility into devig behaviour — NOT the money-path odds→settlement check.
        devig = None
        if isinstance(probe, dict):
            candidate_quality = probe.get("candidateQuality")
            if isinstance(candidate_quality, dict):
                devig = candidate_quality.get("devigDiagnostics")
        store = self.state.store
        odds_stats = store.odds_stats(name) if hasattr(store, "odds_stats") else {}
        payload = {
            "node": name,
            "readonly": config.readonly,
            "latest": latest,
            "manifest": strip_secrets(manifest) if manifest is not None else None,
            "release": strip_secrets(release) if release is not None else None,
            "runtimeProbe": probe,
            "status": strip_secrets(_status_summary(status)),
            "containerState": (latest or {}).get("container_state"),
            "image": (latest or {}).get("image"),
            "startedAt": (latest or {}).get("started_at"),
            "uptimeSecs": (latest or {}).get("uptime_secs"),
            "devigDiagnostics": devig,
            "oddsSamples": {
                "count": odds_stats.get("count", 0),
                "latestTs": odds_stats.get("latest_ts"),
            },
        }
        self._send_json(HTTPStatus.OK, payload)

    def _node_history(self, name: str, query: dict[str, list[str]]) -> None:
        hours = _as_float(query.get("hours", ["24"])[0]) or 24.0
        metrics_arg = query.get("metrics", [""])[0]
        metrics = [metric for metric in metrics_arg.split(",") if metric]
        history = self.state.store.history(name, hours, metrics)
        self._send_json(HTTPStatus.OK, history)

    def _node_odds(self, name: str) -> None:
        latest = self.state.store.latest_odds(name)
        kinds: dict[str, Any] = {}
        for kind, row in latest.items():
            payload = row.get("payload")
            candidates = payload if isinstance(payload, list) else []
            kinds[kind] = {
                "ts_utc": row.get("ts_utc"),
                "candidates": [strip_secrets(candidate) for candidate in candidates],
            }
        self._send_json(HTTPStatus.OK, {"node": name, "kinds": kinds})

    def _get_job(self, job_id: str) -> None:
        job = self.state.jobs.get(job_id)
        if job is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
            return
        self._send_json(HTTPStatus.OK, job)

    # -- auth routes ------------------------------------------------------------

    def _auth_whoami(self) -> None:
        config = self.state.config
        if config.auth_enabled:
            self._send_json(
                HTTPStatus.OK,
                {"username": config.user, "is_default": False},
            )
            return
        auth = self.state.auth
        if auth is None:
            self._send_json(HTTPStatus.OK, {"username": None, "is_default": False})
            return
        self._send_json(HTTPStatus.OK, auth.whoami())

    def _auth_change(self) -> None:
        config = self.state.config
        if config.auth_enabled:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "auth is configured via environment variables; "
                    "change NODEOPS_USER/PASSWORD instead",
                },
            )
            return
        auth = self.state.auth
        if auth is None:
            self._send_json(HTTPStatus.CONFLICT, {"error": "auth is disabled"})
            return
        body = self._read_body()
        current_password = str(body.get("current_password") or "")
        new_password = str(body.get("new_password") or "")
        new_username_raw = str(body.get("new_username") or "").strip()
        if not current_password:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "current_password required"})
            return
        if not (AUTH_MIN_PASSWORD_LEN <= len(new_password) <= AUTH_MAX_PASSWORD_LEN):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"new_password too short (min {AUTH_MIN_PASSWORD_LEN})"},
            )
            return
        # Empty-after-strip means "keep the current username"; only a non-empty but
        # malformed value is a validation error.
        new_username = new_username_raw or None
        if new_username is not None and not _valid_username(new_username):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid new_username"})
            return
        ok, message = auth.change(current_password, new_username, new_password)
        if not ok:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": message})
            return
        identity = auth.whoami()
        # Audit the account change WITHOUT any password material — only the new
        # username (if changed). strip_secrets would already drop *_password keys.
        self._audit(
            "auth.change",
            None,
            {"new_username": new_username} if new_username else {},
            "ok",
        )
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "username": identity["username"], "is_default": identity["is_default"]},
        )

    # -- alerts / audit routes --------------------------------------------------

    def _mask_webhook(self) -> str | None:
        """
        Return the configured webhook URL with everything after the host masked.

        Operators need to confirm *which* endpoint is wired without the full path /
        token leaking into the browser. ``None`` when no webhook is configured.

        """
        url = self.state.config.alert_webhook
        if not url:
            return None
        split = urlsplit(url)
        if split.scheme and split.netloc:
            return f"{split.scheme}://{split.netloc}/…"
        return "…"

    def _list_alerts(self) -> None:
        limit = ALERT_RING_SIZE
        events = self.state.alerts.recent(limit)
        self._send_json(
            HTTPStatus.OK,
            {
                "webhook": self._mask_webhook(),
                "webhook_configured": self.state.config.alert_webhook is not None,
                "alerts": events,
            },
        )

    def _alerts_test(self) -> None:
        if self._readonly_blocked():
            self._audit("alerts.test", None, None, "blocked")
            return
        result = self.state.alerts.fire_synthetic()
        self._audit("alerts.test", None, None, "ok")
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "webhook": self._mask_webhook(),
                "webhook_configured": result["webhook_configured"],
                "event": result["event"],
            },
        )

    def _list_audit(self, query: dict[str, list[str]]) -> None:
        limit_raw = _as_int(query.get("limit", [str(AUDIT_DEFAULT_LIMIT)])[0])
        limit = AUDIT_DEFAULT_LIMIT if limit_raw <= 0 else min(limit_raw, AUDIT_MAX_LIMIT)
        store = self.state.store
        rows = store.recent_audit(limit) if hasattr(store, "recent_audit") else []
        self._send_json(HTTPStatus.OK, {"limit": limit, "rows": rows})

    # -- mutating routes --------------------------------------------------------

    def _deploy_node(self) -> None:
        if self._readonly_blocked():
            self._audit("deploy", None, None, "blocked")
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
        self._audit(
            "deploy",
            container,
            {"image": image, "manifest_path": safe_manifest},
            "accepted",
        )
        self._start_job("deploy", container, args)

    def _node_lifecycle(self, name: str, action: str) -> None:
        if self._readonly_blocked():
            self._audit(action, name, None, "blocked")
            return
        # ``--`` terminates flag parsing so the name can never be read as an option.
        result = _run_docker(["docker", action, "--", name])
        if result is None:
            self._audit(action, name, None, "failed")
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"docker {action} failed"})
            return
        self._audit(action, name, None, "ok")
        self._send_json(HTTPStatus.OK, {"node": name, "action": action, "ok": True})

    def _node_config(self, name: str) -> None:
        """
        Rewrite allow-listed market-window fields in a node's runtime manifest.

        Flow: validate the body against ``CONFIG_ALLOWED_KEYS``; apply the
        updates to a copy of ``manifest.runtime.json`` under its ``strategy``
        block — the location the strategy actually reads — mirroring each value
        to the top level only when that key already exists there, so the two
        copies never contradict; refuse (403) unless the
        result still passes ``manifest_is_validation_safe`` (defence in depth —
        the allow-listed fields cannot arm execution, but re-check anyway); then
        back up the old manifest, atomically write the new one, and optionally
        restart the container so the node re-reads the manifest.

        """
        if self._readonly_blocked():
            self._audit("config", name, None, "blocked")
            return
        body = self._read_body()
        updates, error = validate_config_body(body)
        if updates is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return
        manifest_path = self.state.config.nodes_root / name / "manifest.runtime.json"
        manifest = read_json_file(manifest_path)
        if manifest is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "manifest.runtime.json not found or unreadable"},
            )
            return
        strategy = manifest.get("strategy")
        strategy = dict(strategy) if isinstance(strategy, dict) else {}
        changed = {
            field: {"old": strategy.get(field), "new": value} for field, value in updates.items()
        }
        strategy.update(updates)
        new_manifest = dict(manifest)
        new_manifest["strategy"] = strategy
        new_manifest.update({field: value for field, value in updates.items() if field in manifest})
        if not manifest_is_validation_safe(new_manifest):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "resulting manifest is not validation-safe; nodeops only "
                    "manages data-only nodes (validation_mode with no execution armed)",
                },
            )
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = manifest_path.with_name(f"{manifest_path.name}.bak-{stamp}")
        try:
            backup_path.write_text(manifest_path.read_text(encoding="utf8"), encoding="utf8")
            _atomic_write_manifest(manifest_path, new_manifest)
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"manifest write failed: {exc}"},
            )
            return
        audit_params = {field: change["new"] for field, change in changed.items()}
        restarted = False
        if body.get("restart") is True:
            if _run_docker(["docker", "restart", "--", name]) is None:
                self._audit("config", name, audit_params, "written; restart failed")
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "manifest updated but docker restart failed",
                        "changed": changed,
                        "backup": backup_path.name,
                    },
                )
                return
            restarted = True
        self._audit(
            "config",
            name,
            {**audit_params, "restarted": restarted},
            "ok",
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "node": name,
                "changed": changed,
                "backup": backup_path.name,
                "restarted": restarted,
            },
        )

    def _delete_node(self, name: str) -> None:
        if self._readonly_blocked():
            self._audit("delete", name, None, "blocked")
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
        self._audit("delete", name, None, "accepted")
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


def build_server(
    config: Config,
    store: Store,
    jobs: Jobs,
    auth: AuthStore | None = None,
    alerts: AlertState | None = None,
) -> ThreadingHTTPServer:
    """
    Construct the threading HTTP server with shared state attached.
    """
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.state = NodeOpsState(config, store, jobs, auth, alerts)  # type: ignore[attr-defined]
    return server


def main() -> int:
    """Entry point: start the sampler and serve until SIGTERM/SIGINT."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config()
    # In env-override mode auth comes from NODEOPS_USER/PASSWORD and we never touch
    # the credential store. Otherwise seed the persisted store (admin/admin, hashed)
    # so a fresh install always comes up authenticated — never plaintext, never
    # CHANGE_ME. Store-backed auth also satisfies the public-bind requirement.
    auth: AuthStore | None
    if config.auth_enabled:
        auth = None
    else:
        auth = AuthStore(config.auth_path)
        try:
            auth.seed_default_if_absent()
        except OSError as exc:
            logger.error("could not create credential store at %s: %s", config.auth_path, exc)
            auth = None
    store_authed = auth is not None and auth.load() is not None
    if _insecure_public_bind(config) and not store_authed:
        logger.error(
            "refusing to start: NODEOPS_HOST=%s is not loopback but HTTP Basic auth is "
            "not configured. Set NODEOPS_USER/NODEOPS_PASSWORD, or bind 127.0.0.1.",
            config.host,
        )
        return 2
    if not config.auth_enabled and not store_authed:
        logger.warning(
            "HTTP Basic auth is DISABLED (loopback bind only). Set NODEOPS_USER/"
            "NODEOPS_PASSWORD before exposing this service beyond localhost.",
        )
    store = Store(config.db_path)
    jobs = Jobs()
    alerts = AlertState(config)
    stop_event = threading.Event()
    sampler = Sampler(config, store, stop_event, alerts)
    sampler.start()
    server = build_server(config, store, jobs, auth, alerts)

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
