-- Postgres schema for the nodeops host-side data shipper.
--
-- Mirrors and extends the on-host nodeops SQLite tables (tools/nodeops/server.py
-- Store._create_schema) plus the node-directory artefacts (status.json, node.log,
-- events.jsonl). Every statement is IF NOT EXISTS so the shipper can run it every
-- startup idempotently. Unique constraints give exactly-once semantics: the shipper
-- always inserts with ON CONFLICT DO NOTHING and only advances its cursor after the
-- transaction commits, so a replayed batch never duplicates and a crash never skips.

CREATE TABLE IF NOT EXISTS samples (
    ts_utc                     timestamptz NOT NULL,
    node                       text NOT NULL,
    container_state            text,
    heartbeat_age_secs         double precision,
    image                      text,
    subscribed_instruments     bigint,
    graph_nodes                bigint,
    graph_edges                bigint,
    quoted_edges               bigint,
    semantic_match_instruments bigint,
    cross_venue_candidate_count bigint,
    rag_green                  bigint,
    rag_amber                  bigint,
    rag_red                    bigint,
    raw_detections             bigint,
    valid_opportunities        bigint,
    executable_candidates      bigint,
    executed                   bigint,
    pending_approvals          bigint,
    mem_mb                     double precision,
    cpu_pct                    double precision,
    started_at                 text,
    uptime_secs                double precision,
    PRIMARY KEY (node, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_samples_node_ts
    ON samples (node, ts_utc DESC);

CREATE TABLE IF NOT EXISTS odds_samples (
    id      bigserial PRIMARY KEY,
    ts_utc  timestamptz NOT NULL,
    node    text NOT NULL,
    kind    text NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (node, ts_utc, kind)
);
CREATE INDEX IF NOT EXISTS idx_odds_node_ts
    ON odds_samples (node, ts_utc DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id             bigserial PRIMARY KEY,
    ts_utc         timestamptz NOT NULL,
    username       text,
    action         text NOT NULL,
    node           text,
    params_summary text,
    status         text NOT NULL,
    UNIQUE (ts_utc, action, status, node, username, params_summary)
);
CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON audit_log (ts_utc DESC);

-- Full status.json (runtimeProbe blob) deduped by content hash: an unchanged status
-- collides on (node, content_sha) and is not re-stored.
CREATE TABLE IF NOT EXISTS status_snapshots (
    id          bigserial PRIMARY KEY,
    node        text NOT NULL,
    ts_utc      timestamptz NOT NULL,
    updated_at  timestamptz,
    payload     jsonb NOT NULL,
    content_sha text NOT NULL,
    UNIQUE (node, content_sha)
);
CREATE INDEX IF NOT EXISTS idx_status_node_ts
    ON status_snapshots (node, ts_utc DESC);

CREATE TABLE IF NOT EXISTS node_logs (
    id         bigserial PRIMARY KEY,
    node       text NOT NULL,
    session_id text NOT NULL,
    seq        bigint NOT NULL,
    ts_ingest  timestamptz NOT NULL,
    line       text NOT NULL,
    UNIQUE (node, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_node_logs_node_session_seq
    ON node_logs (node, session_id, seq);

CREATE TABLE IF NOT EXISTS node_events (
    id         bigserial PRIMARY KEY,
    node       text NOT NULL,
    session_id text NOT NULL,
    seq        bigint NOT NULL,
    ts_ingest  timestamptz NOT NULL,
    payload    jsonb NOT NULL,
    UNIQUE (node, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_node_events_node_session_seq
    ON node_events (node, session_id, seq);

-- Persistent high-water marks. Living in Postgres (not a local file) means a restart
-- or a rebuilt host resumes exactly where it left off.
--   source = "sqlite:samples" | "sqlite:odds_samples" | "sqlite:audit_log"
--            -> cursor is the max SQLite rowid shipped
--   source = "log:<node>|<session_id>|<filename>"
--            -> cursor is JSON {"offset": <byte offset>, "seq": <last seq>}
CREATE TABLE IF NOT EXISTS shipper_state (
    source     text PRIMARY KEY,
    cursor     text NOT NULL,
    updated_at timestamptz NOT NULL
);
