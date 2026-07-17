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

-- Flat trade tables flattened from each node's status.json runtimeProbe at ship time.
-- Rows are keyed by the probe's own updatedAt (snapshot_ts), so re-shipping an
-- unchanged status collides on the PK and the upserts stay idempotent.

CREATE TABLE IF NOT EXISTS arb_pnl_samples (
    node                  text NOT NULL,
    snapshot_ts           timestamptz NOT NULL,
    pairs_tracked         bigint,
    pairs_open            bigint,
    pairs_settled         bigint,
    open_exposure         numeric,
    open_guaranteed_pnl   numeric,
    realized_pnl          numeric,
    settlements_received  bigint,
    settlements_unmatched bigint,
    PRIMARY KEY (node, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS live_execution_samples (
    node                 text NOT NULL,
    snapshot_ts          timestamptz NOT NULL,
    kill_switch_active   boolean,
    halt_reason          text,
    realized_loss        numeric,
    notional_used        numeric,
    max_daily_notional   numeric,
    max_daily_loss       numeric,
    attempts             bigint,
    blocks               bigint,
    submissions          bigint,
    block_reasons        jsonb,
    submissions_by_venue jsonb,
    PRIMARY KEY (node, snapshot_ts)
);

-- Pending approvals are entities, not samples: one row per approval_id, mutable odds
-- and stakes updated in place, last_seen = the newest snapshot that carried it.
CREATE TABLE IF NOT EXISTS arb_approvals (
    node                       text NOT NULL,
    approval_id                text NOT NULL,
    canonical_pair_id          text,
    created_at                 timestamptz,
    expires_at                 timestamptz,
    match_type                 text,
    venue_a                    text,
    venue_b                    text,
    instrument_id_a            text,
    instrument_id_b            text,
    market_a                   text,
    market_b                   text,
    outcome_a                  text,
    outcome_b                  text,
    odds_a                     numeric,
    odds_b                     numeric,
    stake_a                    numeric,
    stake_b                    numeric,
    fee_adjusted_profit_margin numeric,
    raw_profit_margin          numeric,
    expected_profit            numeric,
    last_seen                  timestamptz,
    PRIMARY KEY (node, approval_id)
);

CREATE TABLE IF NOT EXISTS arb_approval_stats (
    node               text NOT NULL,
    snapshot_ts        timestamptz NOT NULL,
    mode               text,
    ttl_secs           double precision,
    max_pending        bigint,
    staged             bigint,
    approved_executed  bigint,
    approved_blocked   bigint,
    rejected           bigint,
    expired            bigint,
    evicted            bigint,
    commands_processed bigint,
    commands_invalid   bigint,
    pending_count      bigint,
    recent_decisions   jsonb,
    PRIMARY KEY (node, snapshot_ts)
);

CREATE TABLE IF NOT EXISTS arb_pairs (
    node            text NOT NULL,
    pair_id         text NOT NULL,
    settled         boolean,
    void            boolean,
    fully_hedged    boolean,
    cross_currency  boolean,
    base_currency   text,
    winning_outcome text,
    exposure        numeric,
    guaranteed_pnl  numeric,
    best_case_pnl   numeric,
    realized_pnl    numeric,
    last_seen       timestamptz,
    PRIMARY KEY (node, pair_id)
);

CREATE TABLE IF NOT EXISTS trade_legs (
    node              text NOT NULL,
    pair_id           text NOT NULL,
    client_order_id   text NOT NULL,
    venue             text,
    outcome           text,
    side              text,
    currency          text,
    stake             numeric,
    exposure          numeric,
    fill_count        bigint,
    fills             jsonb,
    settlement_result text,
    last_seen         timestamptz,
    PRIMARY KEY (node, pair_id, client_order_id)
);

-- Persistent high-water marks. Living in Postgres (not a local file) means a restart
-- or a rebuilt host resumes exactly where it left off.
--   source = "sqlite:samples" | "sqlite:odds_samples" | "sqlite:audit_log"
--            -> cursor is JSON {"rowid": <max rowid shipped>, "sha": <sha256 of the
--               spec columns at that rowid>}; a legacy bare-int cursor still parses
--   source = "log:<node>|<session_id>|<filename>"
--            -> cursor is JSON {"offset": <byte offset>, "seq": <last seq>}
CREATE TABLE IF NOT EXISTS shipper_state (
    source     text PRIMARY KEY,
    cursor     text NOT NULL,
    updated_at timestamptz NOT NULL
);
