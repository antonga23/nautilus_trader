# Nodeops dashboard productionisation — build spec (frozen contract)

Status: **frozen**. Branch `feat/nodeops-ui-productionise` off `develop`.
Owners: one backend engineer (`tools/nodeops/server.py`, `tests/unit/tools/test_nodeops.py`),
one frontend engineer (`tools/nodeops/index.html`). They build in parallel against this
document. Where this spec and the code disagree, this spec wins; if the spec is silent on a
detail, the existing behaviour in `server.py` / `index.html` (as of the reading below) wins.

## 0. Scope, non-goals, hard constraints

### 0.1 What we are building

1. Persisted, changeable auth (Basic) backed by a salted-hash credential store, with a
   mandatory first-login password change when still on the shipped default.
2. Derived RAG (green/amber/red) computed from probe fields that actually exist (the node
   never populates `candidateQuality.ragBands` — it is a dead field), plus a node-list RAG
   column and a RAG filter.
3. An edges / opportunities view reading both stored `odds_samples` and the live probe.
4. Working stop / restart / start / delete controls from the UI with confirmation UX and
   async job status.
5. A comprehensive node-detail drawer (tabs/sections) surfacing the full 34-key
   `runtimeProbe`, plus richer node-list columns and filters.

### 0.2 Non-goals — DO NOT build these (safety invariants from PRs #255/#257)

- **No node creation in the UI.** Node creation is pipeline-only. The `POST /api/nodes`
  deploy endpoint MAY stay server-side (its validation-safe gate and tests are unchanged),
  but the frontend MUST NOT render a create/deploy control. The existing `deploy-form` in
  `index.html` is **removed** in this work (see §D.4).
- **No order-placement / execution-arming controls anywhere.** No button, field, or endpoint
  that could arm, dry-run, or trigger execution. The `manifest_is_validation_safe` gate stays
  exactly as-is.
- Keep every existing safety property: manifest-validation-safe deploy gate,
  refuse-public-without-auth (`_insecure_public_bind`), identifier allowlist
  (`valid_name`/`valid_image`), `MAX_BODY_BYTES` body cap, `strip_secrets`, the
  `Handler.state` property, and the strict-JSON sampler (`_read_body` returns `{}` on any
  non-dict / parse failure).

### 0.3 Hard technical constraints

- **Python 3.10 compatible.** No 3.11+-only syntax/APIs. Keep the `try/except` `datetime.UTC`
  shim with `UTC = timezone.utc  # noqa: UP017`. Use `from datetime import UTC` in tests
  (tests run under 3.13 locally per the repo, but must not force 3.11+ features into
  `server.py`).
- **Stdlib only.** New stdlib modules permitted: `hashlib`, `hmac`, `secrets`, `json`,
  `sqlite3`, `http.server`, `tempfile`, `os`. No third-party deps. No new imports beyond
  stdlib.
- **CSP unchanged.** `index.html` is served with:
  `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'`.
  Everything inline/self-contained: no CDNs, no external fonts/scripts/styles. All charts are
  hand-rolled inline SVG (extend the existing `sparkline`). No `fetch` to any host but `self`.
- **Pinned hooks must pass:** `ruff@0.14.10` check + format; `docformatter==1.7.7`
  (`--wrap-summaries 88 --wrap-descriptions 88 --make-summary-multi-line
  --pre-summary-newline --blank`); `add-trailing-comma==4.0.0`; `mypy@1.19.1` (server.py +
  test only); `typos==1.40.0`; `markdownlint-cli2@0.20.0` on this `.md`.
- Run python via:
  `cd <worktree> && PATH="/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH" uv run …`.
  nodeops is pure stdlib (no `nautilus` import) so tests run under any python3.

### 0.4 Interface freeze rules (so the two engineers never disagree)

- **JSON shapes in §C are authoritative.** Backend returns exactly these keys; frontend reads
  exactly these keys. No key is renamed, dropped, or added without editing this spec first.
- Numbers are JSON numbers; timestamps are strings `YYYY-MM-DDTHH:MM:SSZ` (UTC, `Z` suffix),
  matching the existing sampler format.
- All monetary/margin values from the probe are passed through **as the probe emits them**
  (often strings like `"0.031"`); the frontend is responsible for parsing/formatting for
  display and MUST tolerate string-or-number. Backend does not coerce probe candidate fields.
- Every `/api/**` route requires auth when `auth_enabled` (unchanged `_authorized` gate at
  dispatch). Mutating routes additionally require `NODEOPS_READONLY=0` (unchanged
  `_readonly_blocked`).
- Errors: non-2xx responses carry `{"error": "<message>"}`. Frontend `api()` already surfaces
  `.error`. Keep this contract for all new endpoints.

---

## A. AUTH design

### A.1 Overview

Today auth is env-only: `Config.user`/`Config.password` from `NODEOPS_USER`/`NODEOPS_PASSWORD`,
compared with `hmac.compare_digest` against the **plaintext** password in `_authorized`. We add
a **persisted, salted-hash credential store** that:

- seeds `admin`/`admin` (hashed, never plaintext) on first startup with `is_default=true`;
- is changeable via `POST /api/auth/change`;
- exposes identity + default flag via `GET /api/auth/whoami`;
- keeps the env-var override path working (used by every existing auth test).

### A.2 Precedence: env override vs store

`_authorized` resolves credentials in this order:

1. **Env override active** — `config.auth_enabled` is `True` (i.e. `NODEOPS_USER` set and not
   `CHANGE_ME`). Then verify against `config.user` / `config.password` using the **existing
   plaintext `compare_digest` path, unchanged.** The credential store is NOT consulted, is NOT
   required to exist, and is NOT seeded. This preserves every current test
   (`test_auth_accepts_correct_credentials`, etc.) verbatim.
2. **No env override** (`NODEOPS_USER` unset/`CHANGE_ME`). Then:
   - If a credential store exists / can be seeded → auth is **enabled** and verified against
     the store (salted hash, constant-time compare). This is the production default.
   - The old "loopback + no env user ⇒ auth disabled" behaviour is **replaced** by the store:
     on loopback with no env user, we now seed and require the store. See A.9 for the one test
     that changes and the compatibility switch that keeps the rest green.

`Config.auth_enabled` semantics are unchanged (still "env user is set"). A new derived notion,
"effective auth" = env-auth OR store-auth, governs `_authorized`. Backend introduces
`Handler._auth_mode()` returning `"env" | "store" | "disabled"` to make this explicit and
testable.

### A.3 `auth.json` schema

Location: sibling of `NODEOPS_DB`'s directory. `NODEOPS_DB` defaults to
`/opt/cloudbet/nodeops/nodeops.db`, so the store defaults to
`/opt/cloudbet/nodeops/auth.json`. Resolve as:

```text
auth_path = Config.db_path.parent / "auth.json"
```

Add `Config.auth_path` (a `Path`) computed from `db_path.parent`. Overridable for tests via
`NODEOPS_AUTH_FILE` (mirrors the `NODEOPS_DB` override style): if set, use it verbatim;
else `db_path.parent / "auth.json"`.

Exact JSON (object, one record — single admin identity):

```json
{
  "version": 1,
  "username": "admin",
  "algo": "pbkdf2_hmac_sha256",
  "iterations": 200000,
  "salt": "<base64-standard-encoded 16 random bytes>",
  "hash": "<base64-standard-encoded 32-byte derived key>",
  "is_default": true,
  "updated_at": "2026-07-05T00:00:00Z"
}
```

Field rules:

- `version`: integer, currently `1`. Reserved for future migrations; readers reject unknown
  higher versions with a logged warning and treat the store as absent (forces reseed only if
  file was corrupt — see A.7).
- `username`: string, must satisfy the same allowlist idea as node names is NOT required;
  usernames may contain any non-`:` printable; we validate `1..64` chars, no `:` (Basic auth
  delimiter), no control chars. Reject on change otherwise (400).
- `algo`: literal `"pbkdf2_hmac_sha256"`. Any other value ⇒ treat store as unreadable.
- `iterations`: integer `>= 200000`. On verify, use the stored value (so future re-hashes at
  higher cost still verify). On seed/change, write `AUTH_ITERATIONS = 200000`.
- `salt`: base64 (standard, `base64.b64encode`) of exactly 16 bytes from
  `secrets.token_bytes(16)`.
- `hash`: base64 of `hashlib.pbkdf2_hmac("sha256", password_bytes, salt_bytes, iterations)`
  (default dklen 32).
- `is_default`: bool. `true` only for the seeded `admin`/`admin`; set `false` on any
  successful change.
- `updated_at`: UTC timestamp string, same format as samples.

### A.4 Hashing helpers (backend, in `server.py`)

```text
AUTH_ITERATIONS = 200000            # module constant, >= 200k
AUTH_ALGO = "pbkdf2_hmac_sha256"    # module constant

def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf8"), salt, iterations)

def _make_credential(username: str, password: str, *, is_default: bool) -> dict:
    salt = secrets.token_bytes(16)
    derived = _hash_password(password, salt, AUTH_ITERATIONS)
    return {
        "version": 1, "username": username, "algo": AUTH_ALGO,
        "iterations": AUTH_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(derived).decode("ascii"),
        "is_default": is_default,
        "updated_at": <utc now string>,
    }

def _verify_password(record: dict, username: str, password: str) -> bool:
    # constant-time on BOTH username and derived hash
    if record.get("algo") != AUTH_ALGO: return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError): return False
    derived = _hash_password(password, salt, iterations)
    user_ok = hmac.compare_digest(str(record.get("username", "")), username)
    pass_ok = hmac.compare_digest(expected, derived)
    return user_ok and pass_ok
```

`_verify_password` MUST always run the pbkdf2 derivation before returning even when the
username mismatches, to avoid a user-enumeration timing side channel. (Compute `derived`, then
`and` the two `compare_digest` results — do not short-circuit on username.)

### A.5 Credential store (backend)

Introduce `AuthStore` (small class, sibling of `Store`), constructed in `main()` and attached
to `NodeOpsState` as `state.auth` (see A.11). Methods:

- `load() -> dict | None`: read+parse `auth.json`; return `None` on missing/corrupt/unknown-
  version/bad-algo (logged at warning for corrupt, debug for missing).
- `seed_default_if_absent() -> None`: if `load()` is `None`, atomically write
  `_make_credential("admin", "admin", is_default=True)`. Log at INFO:
  `seeded default nodeops credential (admin/admin); change it on first login`.
- `verify(username, password) -> bool`: `record = load(); return record is not None and
  _verify_password(record, username, password)`.
- `whoami() -> dict`: `{"username": record["username"], "is_default": record["is_default"]}`;
  if store missing, `{"username": None, "is_default": False}` (should not happen once seeded).
- `change(current_password, new_username, new_password) -> tuple[bool, str]`: verify current
  against stored record (username taken from stored record — the change form does not send a
  username to verify, only `current_password`); on success write a new credential with
  `is_default=False`, `username = new_username or existing username`, `password =
  new_password`; return `(True, "")`. On current mismatch return `(False, "current password
  incorrect")`.

### A.6 Atomic write (0600)

All writes to `auth.json` go through one helper:

```text
def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auth.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)   # atomic rename on same fs
        os.chmod(path, 0o600)   # ensure final mode 0600 even if umask-affected
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass
        raise
```

`os.replace` is atomic on POSIX same-fs. Final file mode MUST be `0600`.

### A.7 Startup seed

In `main()`, after building `Config` and before/around `Store` construction:

- Only seed when **not** in env-override mode (`not config.auth_enabled`). If env auth is set,
  do not create `auth.json` at all (tests that set `NODEOPS_USER` must not touch the fs auth
  store).
- Call `auth_store.seed_default_if_absent()`. This is what makes a fresh install come up as
  `admin`/`admin` (default), never plaintext, never `CHANGE_ME`.
- The `_insecure_public_bind` refusal is re-evaluated under "effective auth": a public bind is
  allowed if EITHER env auth is set OR the store provides auth (which it always does once
  seeded). Concretely: **effective auth is essentially always enabled now** (store seeds on
  loopback too), so `_insecure_public_bind` returns `True` only in the corner case where the
  store cannot be created/read AND no env auth AND non-loopback host. Keep the existing
  function for the env case; add store-awareness in `main()` (see A.9 for the test impact).

### A.8 `_authorized` change (backend)

Rewrite `_authorized` to:

```text
def _authorized(self) -> bool:
    config = self.state.config
    if config.auth_enabled:
        # env-override path — UNCHANGED plaintext compare_digest
        <existing body verbatim>
    # store path
    auth = self.state.auth
    if auth is None:               # only when explicitly disabled (see A.9)
        return True
    header = self.headers.get("Authorization", "")
    if not header.startswith("Basic "): return False
    try: decoded = base64.b64decode(header[len("Basic "):]).decode("utf8")
    except (ValueError, UnicodeDecodeError): return False
    user, _, password = decoded.partition(":")
    return auth.verify(user, password)
```

The env branch is byte-for-byte the current implementation (so
`test_auth_accepts_correct_credentials` / `_rejects_wrong_credentials` /
`_auth_disabled_when_user_unset` keep passing when they set `NODEOPS_USER`).

### A.9 Compatibility switch for "auth disabled"

`test_auth_disabled_when_user_unset` sets `NODEOPS_USER=""` and asserts `_authorized()` is
`True` with **no** `Authorization` header, and the fake handler builds
`NodeOpsState(config, object(), object())` with **no** auth store. To keep this green without
weakening production:

- `NodeOpsState.__init__` gains a new param `auth: AuthStore | None = None`, defaulting to
  `None`. When `auth is None`, `_authorized`'s store branch returns `True` (disabled). Existing
  fake-handler tests pass `object()` positionally for store/jobs and omit `auth`, so `auth`
  stays `None` ⇒ disabled ⇒ their assertions hold. **This is the deliberate seam.**
- Production `main()` always constructs a real `AuthStore` and passes it, so production is
  always authed.
- **One existing test changes:** none of the current tests must be edited for A.1–A.8 EXCEPT
  where a new test needs a real store. `test_insecure_public_bind_detection` is unaffected
  (it calls `server._insecure_public_bind(config)` directly, which still only inspects env
  auth + host). We do NOT change `_insecure_public_bind`'s signature or the four assertions in
  that test. The store-awareness added in A.7 lives in `main()`, which is not unit-tested for
  the bind decision. **Net: zero edits to existing test assertions.** New auth-store tests are
  additive (§E).

### A.10 New endpoints

Routing additions in `_route` (before the generic node matcher, since these are fixed paths):

- `POST /api/auth/change`
- `GET /api/auth/whoami`

Both require auth (they sit behind the `_authorized` dispatch gate — an unauthenticated caller
gets the 401 challenge). Neither is gated by `NODEOPS_READONLY` (changing your own password is
allowed in read-only mode; read-only governs node mutation, not account security).

#### `GET /api/auth/whoami`

Request: no body.
Response `200`:

```json
{ "username": "admin", "is_default": true }
```

When env-override auth is active (`config.auth_enabled`), return
`{"username": config.user, "is_default": false}` (env creds are never "default").
When auth disabled (store `None`, no env), return `{"username": null, "is_default": false}`.

#### `POST /api/auth/change`

Request body (JSON, dict; `_read_body` rules apply — oversized/non-dict ⇒ treated as `{}`):

```json
{ "current_password": "admin", "new_username": "ops", "new_password": "s3cret-passphrase" }
```

- `new_username` optional; if absent/empty, username is unchanged.
- `current_password` required.
- `new_password` required, min length `8`, max `256`. Reject shorter with 400.
- Validation errors → `400 {"error": "..."}` with messages:
  - `"new_password too short (min 8)"`
  - `"invalid new_username"` (empty-after-strip is treated as "unchanged", not invalid; only
    reject `:`/control chars / >64 chars)
  - `"current_password required"`
- **Env-override mode:** changing the password is not supported (creds come from env). Return
  `409 {"error": "auth is configured via environment variables; change NODEOPS_USER/PASSWORD instead"}`.
- **Auth disabled** (no store, no env): return
  `409 {"error": "auth is disabled"}`.
- Current password wrong → `403 {"error": "current password incorrect"}`.
- Success → `200`:

```json
{ "ok": true, "username": "ops", "is_default": false }
```

On success the write is atomic (A.6) with `is_default=false`. The next `whoami` reflects the
new username and `is_default:false`.

### A.11 `NodeOpsState` / `build_server` wiring

- `NodeOpsState(config, store, jobs, auth=None)`.
- `main()`: `auth = AuthStore(config.auth_path)`; if `not config.auth_enabled:
  auth.seed_default_if_absent()` else `auth = None` (env mode ⇒ no store). Pass `auth` into
  `NodeOpsState`. Actually always pass the constructed store object when not in env mode; pass
  `None` in env mode so `_authorized` uses the env branch anyway (both work, but `None`
  documents intent).
- `Handler.state` unchanged. Add nothing else to `Handler` beyond the new route methods
  `_auth_whoami`, `_auth_change`, and `_auth_mode`.

### A.12 First-login mandatory change UX (trigger contract)

The **backend contract** is only: `whoami.is_default === true` means "still on shipped
default". The **frontend** (see §D.6) MUST, on load, call `GET /api/auth/whoami`; if
`is_default` is true, open a modal change-password dialog that cannot be dismissed until a
successful `POST /api/auth/change`, then re-fetch `whoami` and reload nodes. This is a UI
behaviour, not enforced server-side (the server still serves data to the default admin — we do
not lock out the operator, we nag them).

---

## B. RAG derivation

### B.1 Principle

The node **never** populates `candidateQuality.ragBands` (dead field). `build_sample_row` must
stop reading `ragBands` and instead **derive** green/amber/red from fields that exist. The
derivation is deterministic and documented both here and as a code comment in
`build_sample_row` / a helper `_derive_rag`.

Semantics:

- **GREEN** = profitable AND execution-eligible candidates (real, tradeable edges).
- **RED** = negative-margin candidates (losing / adverse buckets).
- **AMBER** = the remainder — profitable-but-blocked, near-miss, or stale (everything that is
  neither cleanly green nor red).

### B.2 Input fields (all under `runtimeProbe`)

- `candidateQuality.marginBands` — `{bucket: count}`. Buckets include a `"positive"` bucket
  (aggregate profitable count) and negative buckets whose label starts with `"<"` or contains
  a negative sign, e.g. `"< -5%"`, `"-5% to -1%"`, and near-zero-negative like `"0% to -1%"`.
  Treat any bucket whose parsed lower or representative value is `< 0` as negative.
- `strategyStats.executable_candidates` and `strategyStats.opportunities_found`.
- `quotedEdges` (top-level probe int) — used only as a sanity cap for a zero-quote node
  (if `quotedEdges == 0` then GREEN is forced to 0, because nothing is quotable/tradeable).
- `candidateQuality.executionSafeEdges` (int, may be absent) — an explicit count of edges that
  passed safety gating. When present, it caps GREEN.
- (Read but not required for the formula; available for panels:) `quoteObservationState.health`,
  per-candidate `marginBand`/`safetyTier`/`rejectionBucket`.

### B.3 Exact formula (`_derive_rag`)

Given the probe dict, compute three non-negative integers `(green, amber, red)`:

```text
def _derive_rag(probe: dict) -> tuple[int, int, int]:
    cq = probe.get("candidateQuality") or {}
    stats = probe.get("strategyStats") or {}
    margin_bands = cq.get("marginBands") or {}
    quoted = _as_int(probe.get("quotedEdges"))

    # 1. total profitable = the "positive" aggregate bucket if present,
    #    else the sum of all non-negative buckets.
    positive = _as_int(margin_bands.get("positive"))
    if "positive" not in margin_bands:
        positive = sum(_as_int(c) for label, c in margin_bands.items()
                       if not _band_is_negative(label))

    # 2. red = sum of every negative-margin bucket's count.
    red = sum(_as_int(c) for label, c in margin_bands.items()
              if _band_is_negative(label))

    # 3. green = profitable AND execution-eligible, capped.
    #    caps: executionSafeEdges (if present), executable_candidates (if >0),
    #    and forced to 0 when nothing is quoted.
    caps = [positive]
    safe = cq.get("executionSafeEdges")
    if safe is not None:
        caps.append(_as_int(safe))
    exec_cand = _as_int(stats.get("executable_candidates"))
    if exec_cand > 0:
        caps.append(exec_cand)
    green = 0 if quoted == 0 else max(0, min(caps))

    # 4. amber = the profitable-but-not-green remainder (blocked / near-miss / stale),
    #    never negative.
    amber = max(0, positive - green)

    return green, amber, red
```

`_band_is_negative(label)` returns `True` when the bucket label denotes a negative margin.
Rule (documented in code):

```text
def _band_is_negative(label: str) -> bool:
    # "positive" is the aggregate profitable bucket, never negative.
    if label.strip().lower() == "positive":
        return False
    # any explicit minus sign anywhere in the label marks a negative bucket,
    # e.g. "< -5%", "-5% to -1%", "0% to -1%".
    return "-" in label
```

Notes / invariants:

- `marginBands` labels like `"0% to -1%"` contain a `-` ⇒ negative (a near-loss bucket) ⇒
  counts toward RED. This is intentional: "0% to -1%" is a losing bucket.
- If `marginBands` is absent/empty, `positive=0, red=0` ⇒ `(0,0,0)`.
- GREEN is 0 whenever `quotedEdges == 0` regardless of margin buckets (a node that is not
  quoting cannot have a live tradeable edge).
- All three are stored as the existing columns `rag_green`, `rag_amber`, `rag_red`; DB schema
  unchanged.

### B.4 `build_sample_row` change

Replace lines that read `rag_bands`:

```text
# OLD:
rag_bands = candidate_quality.get("ragBands") or {}   # dead field, always empty
...
"rag_green": _as_int(rag_bands.get("green")),
"rag_amber": _as_int(rag_bands.get("amber")),
"rag_red":   _as_int(rag_bands.get("red")),

# NEW:
green, amber, red = _derive_rag(probe)   # ragBands is never populated by the node; derive it
...
"rag_green": green, "rag_amber": amber, "rag_red": red,
```

The existing test `test_build_sample_row_reads_probe_paths` feeds `ragBands:{green:4,...}` and
asserts `(4,2,1)`. That assertion is now wrong under derivation and **is updated** in §E
(this is the single existing test that legitimately changes, because the field it asserts is
being deliberately removed). Its status fields also gain `marginBands` so the derived values
are meaningful. See §E.2 for the exact rewrite.

### B.5 Worked examples

#### Example 1 — quoting node `sxbet`

Probe:

```json
{
  "quotedEdges": 42,
  "candidateQuality": {
    "marginBands": { "positive": 28, "0% to -1%": 9, "-1% to -5%": 5, "< -5%": 3 },
    "executionSafeEdges": 22
  },
  "strategyStats": { "opportunities_found": 28, "executable_candidates": 22 }
}
```

- `positive = 28`.
- `red = 9 + 5 + 3 = 17` (all three negative buckets).
- caps = `[28, 22 (executionSafeEdges), 22 (executable_candidates)]` → `min = 22`;
  `quoted=42 != 0` ⇒ `green = 22`.
- `amber = max(0, 28 - 22) = 6`.
- **Result: green=22, amber=6, red=17.** (22 tradeable, 6 profitable-but-blocked, 17 losing.)

#### Example 2 — zero-quote node `multivenue`

Probe:

```json
{
  "quotedEdges": 0,
  "candidateQuality": {
    "marginBands": { "positive": 4, "0% to -1%": 2 }
  },
  "strategyStats": { "opportunities_found": 4, "executable_candidates": 0 }
}
```

- `positive = 4`.
- `red = 2` (the `"0% to -1%"` bucket).
- caps = `[4]` (no `executionSafeEdges`; `executable_candidates=0` not appended); but
  `quoted == 0` ⇒ `green = 0`.
- `amber = max(0, 4 - 0) = 4`.
- **Result: green=0, amber=4, red=2.** (Nothing tradeable because unquoted; 4 profitable
  candidates stuck in amber, 2 losing.)

---

## C. API — every endpoint with exact response shapes

Legend: **A** = requires auth (all do when authed); **RO** = blocked when
`NODEOPS_READONLY=1`. Method/path exactly as routed in `_route`/`_route_node`.

### C.1 `GET /` and `GET /index.html`  (A)

Serves `index.html` with the CSP header from §0.3. Unchanged. `HEAD` supported.

### C.2 `GET /api/nodes`  (A)

Unchanged shape (frontend adds derived-column rendering + filters client-side, but backend
already returns everything needed). Response `200`:

```json
{
  "readonly": true,
  "nodes": [
    { "node": "sxbet", "latest": { <one full samples row, all SAMPLE_COLUMNS keys> } },
    { "node": "multivenue", "latest": null }
  ]
}
```

`latest` is `null` when no sample exists yet. `latest` keys are exactly `SAMPLE_COLUMNS`
(includes `rag_green/amber/red`, `quoted_edges`, `raw_detections`, `valid_opportunities`,
`executable_candidates`, `executed`, `mem_mb`, `cpu_pct`, `container_state`, `image`, etc.).

> Frontend needs, for the new list columns/filters, some fields NOT in `samples`
> (quote-health reason, margin-band mini-breakdown, unique/dup opportunity counts). Rather
> than widen the sampler schema, the list stays as-is and the **detail** endpoint carries the
> rich fields. The node-list "quote-health badge" and "margin-band mini-breakdown" are
> derived client-side from the columns present (`quoted_edges`, `rag_*`) — see §D.2 for the
> exact client-side derivation, so backend needs **no change** to `/api/nodes`.

### C.3 `GET /api/nodes/<name>`  (A)

Unchanged shape; already returns the full probe. Response `200`:

```json
{
  "node": "sxbet",
  "readonly": true,
  "latest": { <full samples row or null> },
  "manifest": { <manifest.runtime.json, secrets stripped, or null> },
  "runtimeProbe": { <full 34-key runtimeProbe, secrets stripped, or null> },
  "status": { <status.json minus runtimeProbe, secrets stripped, or null> },
  "containerState": "running",
  "image": "ghcr.io/.../node@sha256:..."
}
```

The frontend reads named sub-objects of `runtimeProbe` for the detail tabs (§D.3). Backend
does not reshape the probe; it passes it through `strip_secrets` verbatim. The 34 keys the
frontend relies on (names are the contract — backend must not rename, and the node emits
these):

```text
runtimeProbe.subscribedInstruments, graphNodes, graphEdges, quotedEdges,
             semanticMatchInstruments
runtimeProbe.venueCoverage.edgeCounts | quotedEdgeCounts | candidateCounts   (3x3 matrices)
runtimeProbe.venueCoverage.perVenue[<venue>].{nodeCount, subscriptionCount, gapCount}
runtimeProbe.venueCoverage.crossVenueCandidateCount
runtimeProbe.venueCoverage.crossVenueQuoteReadiness
runtimeProbe.venueCoverage.zeroCandidateBlockerCounts   ({blockerReason: count})
runtimeProbe.venueCoverage.zeroCandidateVenuePairs[]     ({venueA, venueB, blockerReason,
                                                          sampleEventKeys[]})
runtimeProbe.strategyStats.{raw_arbitrage_detections, opportunities_found,
             executable_candidates, opportunities_executed, <suppression counters...>}
runtimeProbe.candidateQuality.marginBands | rejectionBuckets | timingFlags   ({label: count})
runtimeProbe.candidateQuality.devigDiagnostics
runtimeProbe.candidateQuality.executionSafeEdges
runtimeProbe.candidateQuality.topPositiveCandidates[] | topNegativeNearMisses[]
             | topValueEdgeCandidates[]
runtimeProbe.quoteObservationState.{health, ...}
runtimeProbe.providerQuotePollStats.<venue>.{quote_count, backlog, latency, last_error}
runtimeProbe.latencyDiagnostics
runtimeProbe.sloStatus
runtimeProbe.coverageDiagnostics
runtimeProbe.semanticDiagnostics
runtimeProbe.feePolicy
runtimeProbe.executionReadiness
```

The frontend treats **every** one of these as optional (any may be absent for a given node);
render "—" / hide the panel when absent. Backend guarantees only that when the node emits a
field, it is passed through unchanged (minus secret-looking keys).

Each candidate object in `topPositiveCandidates` / `topNegativeNearMisses` /
`topValueEdgeCandidates` has this shape (contract — frontend table columns bind to these):

```json
{
  "instrumentPair": "TEAM_A_WIN",
  "venues": ["CLOUDBET", "SXBET"],
  "marketFamily": "match_odds",
  "profitMargin": "0.031",
  "rawProfitMargin": "0.045",
  "feeAdjustedProfitMargin": "0.028",
  "oddsA": "2.10",
  "oddsB": "2.05",
  "marginBand": "positive",
  "candidateValueClassification": "executable",
  "rejectionBucket": null,
  "blockerReason": null,
  "safetyTier": "tier1",
  "templateId": "same-event-two-way"
}
```

Values may be strings or numbers; frontend tolerates both (§0.4). `venues` is an array (render
joined). Any field may be absent → render "—".

### C.4 `GET /api/nodes/<name>/history`  (A)

Unchanged. Query: `hours` (float, default 24), `metrics` (comma list, filtered against
`HISTORY_METRICS`). Response `200`:

```json
{ "node": "sxbet", "hours": 6.0, "metrics": ["graph_edges", "..."],
  "points": [ { "ts_utc": "...Z", "graph_edges": 18, "...": 0 } ] }
```

`HISTORY_METRICS` already includes `raw_detections` and `executable_candidates`, so the new
sparklines (§D.3) require **no backend change** — the frontend just adds them to
`CHART_METRICS`.

### C.5 `GET /api/nodes/<name>/odds`  (A)  — NEW

Returns the latest stored candidate rows from `odds_samples` (what the sampler persisted) so
the operator can see edges even between live probes. Live candidates come from
`GET /api/nodes/<name>` (`runtimeProbe.candidateQuality.top*`); this endpoint is the
**stored/historical** companion.

Routing: extend the node action regex to include `/odds`:

```text
node_match = re.fullmatch(
    r"/api/nodes/([^/]+)(/history|/odds|/restart|/stop|/start)?", path)
```

and in `_route_node`: `if method == "GET" and action == "/odds": self._node_odds(name); return`.

Implementation: add `Store.latest_odds(node) -> dict` returning the most recent row **per
`kind`** for the node:

```text
SELECT o.* FROM odds_samples o
JOIN (SELECT kind, MAX(id) AS max_id FROM odds_samples WHERE node = ? GROUP BY kind) latest
  ON o.id = latest.max_id
```

Response `200`:

```json
{
  "node": "sxbet",
  "kinds": {
    "topPositiveCandidates": {
      "ts_utc": "2026-07-05T00:00:00Z",
      "candidates": [ { <candidate object, see C.3> } ]
    },
    "topNegativeNearMisses": {
      "ts_utc": "2026-07-05T00:00:00Z",
      "candidates": [ { ... } ]
    }
  }
}
```

- `candidates` is the parsed `payload_json` (a list; if stored payload was not a list, wrap or
  return `[]`). Run each candidate through `strip_secrets` defensively before returning.
- If no odds rows exist for the node, `kinds` is `{}` (empty object), status still `200`.
- Only the two kinds the sampler writes today (`topPositiveCandidates`,
  `topNegativeNearMisses`) will appear. The sampler is **extended** to also persist
  `topValueEdgeCandidates` (add it to the tuple in `_record_odds_samples`), so a third kind
  key may appear once samples accrue. Frontend renders whatever kinds are present.

### C.6 `POST /api/nodes`  (A, RO)  — deploy, SERVER-SIDE ONLY

Unchanged behaviour and validation-safe gate. **The frontend no longer calls this** (§D.4).
Kept server-side so the pipeline path and existing deploy tests remain intact. Response `202`
`{"job_id","kind","target"}`; gate rejections `400`/`403` as today.

### C.7 `POST /api/nodes/<name>/{restart,stop,start}`  (A, RO)

Unchanged. Runs `docker <action> -- <name>`. Response `200`:

```json
{ "node": "sxbet", "action": "restart", "ok": true }
```

Failure → `502 {"error": "docker <action> failed"}`. `read-only` → `403 {"error":"read-only mode"}`.

### C.8 `DELETE /api/nodes/<name>`  (A, RO)

Unchanged. Archives-then-removes via the archive script as a background job. Response `202`:

```json
{ "job_id": "…hex…", "kind": "archive", "target": "sxbet" }
```

### C.9 `GET /api/jobs/<hex>`  (A)

Unchanged. Response `200`:

```json
{ "id":"…","kind":"archive","target":"sxbet","state":"running|succeeded|failed",
  "returncode": null, "stdout":"", "stderr":"", "created_at":"…Z", "finished_at":"…Z" }
```

`finished_at` present only once finished. Unknown job → `404 {"error":"unknown job"}`.

### C.10 Error/status summary

| Status | When |
| --- | --- |
| 200 | successful GET / lifecycle / whoami / auth change |
| 202 | job accepted (deploy, delete) |
| 400 | bad node name, bad image, bad manifest_path, invalid auth-change fields |
| 401 | missing/invalid Basic auth (challenge) |
| 403 | read-only mutation; wrong current password on auth change |
| 404 | unknown route / unknown job |
| 409 | auth change while env-auth or auth-disabled |
| 500 | unexpected handler error `{"error":"internal error"}` |
| 502 | docker lifecycle failed |

---

## D. Frontend information architecture (`index.html`)

Everything inline; CSP as §0.3. Extend the existing vanilla-JS SPA — do not restructure the
build. Keep `esc()`, `api()`, `sparkline()`, `fmtAge()`, `stateChip()`, `shortImage()` as-is
and reuse them.

### D.1 Node-list columns (table)

Order (left→right). Existing columns kept; new ones marked **NEW**:

1. Node
2. State (chip)
3. HB age (stale styling if >180s)
4. Image (short)
5. **Quote health** **NEW** — a small badge derived client-side (see D.2). Explains
   `quoted=0`.
6. Instr (`subscribed_instruments`)
7. Edges (`graph_edges`)
8. Quoted (`quoted_edges`)
9. xVenue (`cross_venue_candidate_count`)
10. RAG (`rag_green`/`rag_amber`/`rag_red`) — existing `ragCell`, now meaningful (derived
    backend-side).
11. **Margin mini** **NEW** — a 3-segment inline-SVG bar (green/amber/red proportional to
    `rag_*`), width ~64px, rendered next to RAG or as its own cell. Hand-rolled SVG rects (CSP
    safe).
12. Raw (`raw_detections`)
13. Valid (`valid_opportunities`)
14. Exec (`executable_candidates`)
15. Done (`executed`)
16. **Uniq/Dup** **NEW** — unique/duplicate opportunity counts. Derived from
    `valid_opportunities` (unique) vs `raw_detections - valid_opportunities` (duplicate/
    suppressed) as a lightweight proxy, rendered `uniq/dup`. (Documented as a proxy in a code
    comment; if the probe later exposes explicit counts they can be swapped in via the detail
    endpoint.)

Update the `colspan` in the empty/loading `<tr>` to the new column count. Column count moves
from 13 → 16; use `colspan="16"`.

### D.2 Node-list filters (client-side, above the table)

A filter bar (inline styled) with these controls. All filtering is client-side over the
already-fetched `data.nodes` array (no new endpoints; re-render `#rows` on change):

- **RAG filter** (select): `all` | `green>0` (any tradeable) | `amber-dominant`
  (`amber >= green && amber >= red && amber > 0`) | `red-dominant`
  (`red >= green && red >= amber && red > 0`) | `none` (`green+amber+red == 0`).
- **Quote health** (select): `all` | `quoting` (`quoted_edges > 0`) | `no-quotes`
  (`quoted_edges == 0`).
- **Venue** (text/select): substring match on node name (node names encode venue, e.g.
  `sxbet`, `multivenue`). Free-text `contains` filter on `node`.
- **Node family** (select, derived): group by the node-name prefix before the first `-`/`_`
  (or a small fixed list built from the loaded node names). `all` default.

Quote-health **badge** derivation (list column 5), pure client-side from the sample row `s`:

```text
if s.quoted_edges == null    -> badge "—"        (muted)      "no sample"
else if s.quoted_edges > 0   -> badge "quoting"  (pos color)  title: "<n> quoted edges"
else if s.graph_edges > 0    -> badge "no quotes"(warn color) title: "edges exist but 0 quoted — check providerQuotePollStats"
else                         -> badge "no edges" (neg color)  title: "no graph edges built yet"
```

Filters and badges introduce **no** backend dependency.

### D.3 Detail drawer — tabbed / sectioned layout

The drawer becomes tabbed. Keep the drawer shell (`#scrim`, `#drawer`, close button, escape/
scrim-close). Add an inline tab strip under the `<h2>`/sub line. Tabs (in order), each backed
by named `runtimeProbe` fields from `GET /api/nodes/<name>`:

**Tab 1 — Overview**

- State chip + image + heartbeat age + latest sample summary.
- RAG summary (green/amber/red) with the margin mini-bar (reuse D.1 SVG).
- Arb funnel (from `strategyStats`): a horizontal funnel
  `raw_arbitrage_detections → opportunities_found → executable_candidates →
  opportunities_executed`, plus any suppression counters listed below the funnel as
  `label: count` rows. Hand-rolled inline-SVG bars (widths proportional to the top value).

**Tab 2 — Metrics history** (existing charts, extended)

- Extend `CHART_METRICS` to include `raw_detections` ("raw detections") and
  `executable_candidates` ("executable cand") alongside the existing five. Existing
  `loadCharts`/`sparkline`/window buttons (1h/6h/24h) unchanged.

**Tab 3 — Edges / Opportunities** **NEW**

- Two data sources merged: **live** from `detail.runtimeProbe.candidateQuality.{topPositive
  Candidates, topNegativeNearMisses, topValueEdgeCandidates}` and **stored** from
  `GET /api/nodes/<name>/odds` (`kinds.<kind>.candidates`).
- A section per kind (Positive / Negative near-misses / Value edges), each a **sortable
  table** with columns: Instrument pair, Venues (joined), Market family, Profit margin,
  Raw margin, Fee-adj margin, Odds A, Odds B, Margin band, Value class, Rejection/blocker,
  Safety tier, Template. Bind to the candidate object fields in §C.3. Sort client-side on any
  numeric/text column (parse string margins with `parseFloat`, tolerate NaN → sort last).
- Show the source + `ts_utc` (live vs stored-at) per section.

**Tab 4 — Venue coverage** **NEW**

- Per-venue coverage matrices: render `venueCoverage.edgeCounts`, `quotedEdgeCounts`,
  `candidateCounts` as three small 3×3 (venue×venue) grids/tables (labels from the matrix
  keys; missing cells → "—").
- Per-venue counts: `venueCoverage.perVenue[<venue>].{nodeCount, subscriptionCount, gapCount}`
  as a small table.
- Cross-venue quote readiness: `venueCoverage.crossVenueQuoteReadiness` (render its keys/
  values as labelled rows).

**Tab 5 — Why no cross-venue arbs** **NEW** (zero-candidate blockers)

- `venueCoverage.zeroCandidateBlockerCounts` → `{blockerReason: count}` as a sorted bar list.
- `venueCoverage.zeroCandidateVenuePairs[]` → a table: Venue A, Venue B, Blocker reason,
  Sample event keys (join `sampleEventKeys`, truncate with title tooltip).
- `crossVenueCandidateCount` shown as the headline number.

**Tab 6 — Quote health**  **NEW**

- `quoteObservationState` (render `health` prominently + remaining keys as rows).
- `providerQuotePollStats.<venue>` → table: Venue, quote_count, backlog, latency, last_error
  (last_error in warn/neg color when non-empty).

**Tab 7 — Bands** **NEW**

- `candidateQuality.marginBands`, `rejectionBuckets`, `timingFlags` — three labelled
  `{label: count}` breakdowns, each a small sorted bar list (inline SVG or CSS-width bars —
  CSS width via inline style is CSP-safe).

**Tab 8 — Latency / SLO** **NEW**

- `latencyDiagnostics` and `sloStatus` rendered as labelled rows / key-value tables. SLO
  breaches highlighted (neg color) when a status value indicates a breach.

**Tab 9 — Diagnostics** **NEW**

- `candidateQuality.devigDiagnostics`, `coverageDiagnostics`, `semanticDiagnostics` — each a
  collapsible `<pre>`/key-value block (reuse the `pre.manifest` styling). These are
  informational dumps; render generically (recursive key/value or pretty-printed JSON via
  `esc(JSON.stringify(...,2))`).

**Tab 10 — Policy** **NEW** (read-only display)

- `feePolicy` and `executionReadiness` rendered as **read-only** labelled rows. NO controls,
  NO toggles — display only (execution-arming is a non-goal).

**Tab 11 — Manifest**

- Existing `pre.manifest` block (`detail.manifest`).

**Tab 12 — Controls** (see D.4)

Rendering rule: every tab tolerates absent data — if the backing `runtimeProbe` sub-object is
missing/empty, render a muted "no data" line (do not hide the tab, so the operator knows the
field exists but is empty). Generic renderers (recursive key/value) handle unknown nested
shapes so the UI never breaks on a probe field this spec did not enumerate.

### D.4 Controls UX (Tab 12) — CREATE REMOVED, lifecycle wired

- **Remove the deploy form entirely** (the `#deployForm` block and its submit handler in
  `wireActions`, and the `deploy-form` CSS may be dropped). No create/deploy control ships in
  the UI. `POST /api/nodes` remains server-side only.
- Buttons: **Restart**, **Stop**, **Start**, **Delete** (danger). All disabled when
  `detail.readonly` is true, with a "(read-only)" note (existing behaviour).
- **Confirmation UX:**
  - Restart / Stop / Start → simple `confirm()` dialog: `"Restart node <name>?"` etc.
  - Delete → **typed-name confirmation**: a small inline form (not `confirm()`) that shows
    `Type the node name to confirm deletion: <name>` with a text input; the Delete-confirm
    button stays disabled until the typed value === node name (strict equality). Only then
    issue `DELETE /api/nodes/<name>`. This replaces the current one-line `confirm()` for
    delete.
- **Async job status:** reuse `pollJob(jobId, el)` for delete (archive) — it polls
  `GET /api/jobs/<id>` until `state !== "running"`, printing `kind [state] rc=… stdout/stderr`
  into `#job`. Restart/stop/start are synchronous (`200 {ok:true}`) → show `"<action>: ok"`
  and refresh the list after ~1s (existing behaviour). On error, show `"<action> failed: <msg>"`.

### D.5 Refresh / state

- Keep `REFRESH_MS = 15000` list auto-refresh and the per-open-node chart refresh.
- `state` object gains: `authIsDefault` (bool), `filters` (`{rag, quote, venue, family}`),
  `tab` (current drawer tab), and the existing `window`. Re-render `#rows` when filters change
  without refetching.

### D.6 First-login password change (mandatory nag)

- On initial load (before/parallel to `loadNodes`), call `GET /api/auth/whoami`.
- If `whoami.is_default === true`: open a modal dialog (reuse the scrim; a centered inline
  panel) titled "Change the default password". Fields: current password (prefill hint "admin"
  is fine but do not prefill the value), new username (optional), new password, confirm new
  password (client checks match + min length 8). The modal has no close button and ignores
  scrim-click / Escape until success.
- Submit → `POST /api/auth/change {current_password, new_username?, new_password}`. On `200`,
  close the modal, re-call `whoami`, then `loadNodes()`. On `400/403/409`, show the
  `.error` message inline and keep the modal open.
- If `whoami.is_default === false` (or `username === null` when auth disabled), skip the modal
  entirely.
- Because the page is behind Basic auth, the browser already holds credentials; the
  change-password `POST` is authenticated by the same Basic header. After a successful
  username/password change, the browser's cached Basic credentials become stale — show a
  one-line notice: "Password changed. You may be prompted to re-authenticate on next reload."
  (No forced reload; the current session's cached header keeps working until the browser drops
  it.)
- Add a small "Change password" affordance in the header (opens the same dialog) so a non-
  default user can still rotate credentials.

### D.7 CSP / self-containment reminders

- No external fonts/scripts/styles/images. All new charts are inline SVG (`polyline`, `rect`,
  `line`) or CSS-width bars via inline `style` (allowed: `style-src 'unsafe-inline'`).
- All XHR via the existing `api()` (`fetch` same-origin). No new hosts.
- All dynamic HTML goes through `esc()` for any server/probe-derived string (candidate fields,
  venue names, blocker reasons, event keys, error messages). This is mandatory — probe strings
  are untrusted-ish and the CSP is the backstop, not the only defence.

---

## E. Test plan

Location: `tests/unit/tools/test_nodeops.py` (extend). Pure stdlib; runs under any python3.
The existing 30-ish tests stay green **except the two documented rewrites below**. Run:

```text
cd <worktree> && PATH="/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH" \
  uv run python -m pytest tests/unit/tools/test_nodeops.py -q
```

### E.1 Unchanged existing tests (must stay green)

All store/history/validator/secret-stripping/deploy-gate/bind/readonly/real-HTTP tests are
untouched. In particular the env-auth tests (`test_auth_accepts_correct_credentials`,
`_rejects_wrong_credentials`, `_auth_disabled_when_user_unset`) rely on the env branch of
`_authorized` and the `auth=None` default of `NodeOpsState` — do not regress these.

### E.2 Rewritten existing test — `test_build_sample_row_reads_probe_paths`

Because `ragBands` is now derived, this test's status fixture drops `ragBands` and adds
`marginBands` + `executionSafeEdges`, and asserts the **derived** RAG. New assertions:

- Status `runtimeProbe.candidateQuality` = `{"marginBands": {"positive": 6, "0% to -1%": 1},
  "executionSafeEdges": 4}`, `quotedEdges: 7`, `strategyStats.executable_candidates: 6`.
- Assert `(rag_green, rag_amber, rag_red) == (4, 2, 1)`:
  `positive=6`, caps=`[6, 4(safe), 6(exec)]`→green=4 (quoted!=0); amber=`6-4=2`; red=`1`.
- Keep all other assertions (`subscribed_instruments`, `graph_edges`, `heartbeat_age_secs`,
  etc.) identical. This preserves the numeric `(4,2,1)` triple the test historically checked
  while proving the derivation, not the dead field.

### E.3 New unit tests — RAG derivation (`_derive_rag`)

- `test_derive_rag_quoting_node`: Example 1 (§B.5) → `(22, 6, 17)`.
- `test_derive_rag_zero_quote_forces_green_zero`: Example 2 → `(0, 4, 2)`.
- `test_derive_rag_no_margin_bands`: empty/absent `marginBands` → `(0, 0, 0)`.
- `test_derive_rag_band_negativity`: assert `_band_is_negative("positive") is False`,
  `_band_is_negative("0% to -1%") is True`, `_band_is_negative("< -5%") is True`,
  `_band_is_negative("1% to 5%") is False`.
- `test_derive_rag_caps_by_executable_when_no_safe_edges`: `positive=10`,
  `executable_candidates=3`, no `executionSafeEdges`, `quoted=5` → green=3, amber=7.

### E.4 New unit tests — auth store

Use `tmp_path` and `NODEOPS_AUTH_FILE`/`NODEOPS_DB` overrides so nothing touches
`/opt/cloudbet`.

- `test_authstore_seed_default_creates_hashed_admin`: after `seed_default_if_absent`, file
  exists, mode is `0600` (`stat().st_mode & 0o777 == 0o600`), JSON has
  `username=="admin"`, `is_default is True`, `algo=="pbkdf2_hmac_sha256"`, `iterations>=200000`,
  and **no** `password`/plaintext field. `verify("admin","admin") is True`;
  `verify("admin","wrong") is False`; `verify("nope","admin") is False`.
- `test_authstore_seed_is_idempotent`: calling seed twice does not overwrite (same salt/hash).
- `test_authstore_change_success_sets_non_default`: `change("admin","ops","new-pass-123")`
  returns `(True,"")`; reload → `username=="ops"`, `is_default is False`;
  `verify("ops","new-pass-123") is True`; `verify("admin","admin") is False`.
- `test_authstore_change_wrong_current`: `change("bad", None, "new-pass-123")` → `(False, ...)`;
  store unchanged (still default admin).
- `test_authstore_change_keeps_username_when_new_username_absent`: `change("admin", "",
  "new-pass-123")` keeps `username=="admin"`, `is_default False`.
- `test_authstore_atomic_write_mode_0600`: after `change`, file mode is `0600`.
- `test_verify_password_constant_time_shape`: `_verify_password` returns bool for mismatched
  algo (`False`) and corrupt base64 (`False`) without raising.
- `test_hash_is_not_plaintext`: the on-disk `hash` field, base64-decoded, does not equal the
  password bytes and is 32 bytes long.

### E.5 New handler tests — whoami / change (fake handler)

Extend the `_FakeHandler` pattern (add `_auth_whoami`, `_auth_change`, `_read_body`, and a real
`AuthStore` on the state via `NodeOpsState(config, store, jobs, auth=authstore)`).

- `test_whoami_reports_default_true`: fresh seeded store → `_send_json` payload
  `{"username":"admin","is_default":True}`, status 200.
- `test_whoami_after_change_is_default_false`: change then whoami → `is_default False`,
  new username.
- `test_auth_change_rejects_short_password`: body `new_password:"short"` (<8) → 400,
  error mentions "too short".
- `test_auth_change_wrong_current_403`: wrong `current_password` → 403.
- `test_auth_change_success_200`: valid change → 200 `{"ok":True,...}` and store updated.
- `test_auth_change_env_mode_409`: with `NODEOPS_USER`/`PASSWORD` set (env mode, `auth=None`
  or a flag), change → 409.
- `test_authorized_uses_store_when_no_env`: build a `NodeOpsState` with a seeded `AuthStore`
  and no env user; `_FakeHandler` with correct Basic header → `_authorized() is True`; wrong
  password → `False`; missing header → `False`.

### E.6 New unit tests — `/odds` store + endpoint

- `test_latest_odds_returns_latest_per_kind`: insert two `topPositiveCandidates` rows (older,
  newer) and one `topNegativeNearMisses`; `Store.latest_odds(node)` returns the newer positive
  payload and the negative payload, keyed by kind.
- `test_latest_odds_empty_for_unknown_node`: returns `{}`/empty for a node with no odds.
- `test_sampler_records_value_edge_candidates`: extend the sampler test — status with
  `candidateQuality.topValueEdgeCandidates` present → an `odds_samples` row with
  `kind=="topValueEdgeCandidates"` is written (proves the tuple extension in C.5).
- Handler-level: a fake `_node_odds` test asserting the response shape `{"node","kinds":{...}}`
  with `candidates` being the parsed list and secrets stripped from candidate dicts.

### E.7 Controls gating tests

- `test_lifecycle_blocked_in_readonly`: `_FakeHandler` (or the lifecycle method) with
  `NODEOPS_READONLY=1` → `_readonly_blocked()` True / 403 (reuse existing pattern; add explicit
  coverage that `/stop`,`/start`,`/restart` and `DELETE` all short-circuit on readonly).
- `test_lifecycle_ok_calls_docker` (monkeypatch `server._run_docker` to return non-None) →
  200 `{"ok":True,"action":...}`; docker failure (return None) → 502.

### E.8 Real-HTTP smoke (extend `test_real_http_server_serves_nodes_and_index`)

Add a second real-server test (or extend) that seeds an `AuthStore` and drives the wire with
Basic auth:

- `test_real_http_auth_and_whoami`: build server with a seeded store (no env user), start on
  ephemeral port. `GET /api/nodes` with **no** auth header → `401`. With
  `Authorization: Basic base64("admin:admin")` → `200`. `GET /api/auth/whoami` (authed) →
  `200 {"username":"admin","is_default":true}`. `POST /api/auth/change` with body
  `{"current_password":"admin","new_password":"changed-pass-1"}` → `200`; subsequent
  `whoami` → `is_default:false`. `GET /api/nodes/<name>/odds` (authed) → `200` with a `kinds`
  object.
- Keep the existing readonly/index assertions.

### E.9 Lint/type gates (run before claiming done)

```text
cd <worktree> && PATH="/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH" \
  uvx ruff@0.14.10 check tools/nodeops/server.py tests/unit/tools/test_nodeops.py && \
  uvx ruff@0.14.10 format --check tools/nodeops/server.py tests/unit/tools/test_nodeops.py && \
  uvx mypy@1.19.1 tools/nodeops/server.py tests/unit/tools/test_nodeops.py && \
  uvx docformatter==1.7.7 --wrap-summaries 88 --wrap-descriptions 88 \
      --make-summary-multi-line --pre-summary-newline --blank \
      --check tools/nodeops/server.py && \
  uvx add-trailing-comma==4.0.0 tools/nodeops/server.py tests/unit/tools/test_nodeops.py && \
  uvx typos==1.40.0 tools/nodeops/ docs/ops/ && \
  uvx markdownlint-cli2@0.20.0 docs/ops/nodeops-ui-spec.md
```

`index.html` is not linted by the python hooks; validate it via the real-HTTP smoke (index
served, non-empty) and manual browser check against a running server.

---

## F. Parallel-work contract summary (the freeze)

- **Backend owns:** `_derive_rag` + `_band_is_negative`; `build_sample_row` change;
  `AuthStore` and hashing/atomic-write helpers; `Config.auth_path` + `NODEOPS_AUTH_FILE`; `NodeOpsState.auth`
  param; `_authorized` rewrite (env branch verbatim); routes `/api/auth/whoami`,
  `/api/auth/change`, `/api/nodes/<name>/odds`; `Store.latest_odds`; sampler
  `topValueEdgeCandidates`; all §E tests. **No renamed JSON keys.**
- **Frontend owns:** node-list columns (D.1) + filters (D.2); tabbed detail drawer (D.3);
  controls (D.4, deploy form **removed**, typed-name delete); first-login change-password nag
  (D.6); header "change password". Reads only the JSON in §C. **No create/deploy control, no
  execution controls, CSP-safe inline everything.**
- **Shared truth:** §C JSON shapes and the candidate-object shape (§C.3). Neither engineer
  changes a key without editing §C first.
