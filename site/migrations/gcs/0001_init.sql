-- The whole D1 schema of oa-gcs-usage-auth, as one baseline (squashed
-- 2026-09-25 from 31 incremental files; an existing database was rebaselined
-- with @open-athena/auth's `scripts/d1-rebaseline.mjs`, which proves the live
-- schema matches this file before rewriting wrangler's bookkeeping).
--
-- Two families of tables live here:
--   · the @open-athena/auth tables (its `migrations/0001_init.sql`, copied so
--     this app owns one migration sequence): grants, access_log(_daily),
--     access_requests, profiles, pending_auth;
--   · the app's own: the sign-in allowlist and identity maps, the marks +
--     actions ledger, cached totals, the path-index metadata, deletion plans and
--     executed sweeps.
-- Sections say what each table is for, not how it got here.

------------------------------------------------------------------------------
-- @open-athena/auth: share links, sessions, audit, access requests, profiles
------------------------------------------------------------------------------

-- Share links. A grant is a revocable capability: its token (stored hashed)
-- redeems for a session that re-joins this row on every request, so revoking
-- the row ends every session it minted. `subject_json` is who the link is for
-- (`{ name?, email?, avatar? }`), `name` the admin's own label for it.
-- `disabled_at` stops new redemptions but leaves live sessions alone;
-- `expiry_ends_sessions` says whether `expires_at` also ends derived sessions;
-- `sessions_invalid_before` is stamped by a rotate that boots old sessions.
CREATE TABLE grants (
  id                      TEXT PRIMARY KEY,
  token_hash              TEXT NOT NULL UNIQUE,
  name                    TEXT,
  note                    TEXT,
  subject_json            TEXT,
  email                   TEXT,
  scopes                  TEXT NOT NULL,          -- space-separated
  max_redeems             INTEGER,
  redeems                 INTEGER NOT NULL DEFAULT 0,
  expires_at              INTEGER,
  session_ttl             INTEGER,
  created_at              INTEGER NOT NULL,
  created_by              TEXT NOT NULL,
  revoked_at              INTEGER,
  first_used_at           INTEGER,
  last_used_at            INTEGER,
  disabled_at             INTEGER,
  expiry_ends_sessions    INTEGER NOT NULL DEFAULT 1,
  sessions_invalid_before INTEGER
);
CREATE INDEX grants_created_at ON grants (created_at DESC);
CREATE INDEX grants_disabled_at ON grants (disabled_at);

-- Audit trail of sign-ins, redeems, denials and gated reads. `bucket` is the
-- time bucket the dedupe index folds repeated reads into.
CREATE TABLE access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  event       TEXT NOT NULL,
  grant_id    TEXT,
  session_sub TEXT,
  path        TEXT,
  status      INTEGER,
  ip_hash     TEXT,
  ua          TEXT,
  country     TEXT,
  referer     TEXT,
  reason      TEXT,
  bucket      INTEGER
);
CREATE UNIQUE INDEX access_log_dedupe ON access_log (event, session_sub, path, bucket) WHERE bucket IS NOT NULL;
CREATE INDEX access_log_grant ON access_log (grant_id, ts DESC);
CREATE INDEX access_log_ts ON access_log (ts DESC);

-- Daily rollup of access_log, so per-link "who used it" views don't scan rows.
CREATE TABLE access_log_daily (
  day      INTEGER NOT NULL,
  event    TEXT NOT NULL,
  grant_id TEXT,
  path     TEXT,
  country  TEXT,
  events   INTEGER NOT NULL,
  clients  INTEGER NOT NULL,
  PRIMARY KEY (day, event, grant_id, path, country)
);
CREATE INDEX access_log_daily_day ON access_log_daily (day DESC);
CREATE INDEX access_log_daily_grant ON access_log_daily (grant_id, day DESC);

-- "Request access" submissions from the sign-in wall; at most one pending per
-- email. A decision records who decided and, on approval, the grant minted.
CREATE TABLE access_requests (
  id           TEXT PRIMARY KEY,
  email        TEXT NOT NULL,
  name         TEXT,
  note         TEXT,
  created_at   INTEGER NOT NULL,
  status       TEXT NOT NULL,        -- 'pending' | 'approved' | 'denied'
  decided_at   INTEGER,
  decided_by   TEXT,
  grant_id     TEXT,
  ip_hash      TEXT,
  subject_json TEXT
);
CREATE INDEX access_requests_created_at ON access_requests (created_at DESC);
CREATE INDEX access_requests_email ON access_requests (email, created_at DESC);
CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (email) WHERE status = 'pending';
CREATE INDEX access_requests_status ON access_requests (status, created_at DESC);

-- A signed-in person's self-set name and face, keyed by email (the identity a
-- Google id_token and an emailed code share). `avatar` is a data: URI or an
-- asset:// ref, never a live remote URL.
CREATE TABLE profiles (
  email      TEXT PRIMARY KEY,
  name       TEXT,
  avatar     TEXT,
  avatar_src TEXT,                    -- 'upload' | 'url' | 'github' | 'gravatar'
  updated_at INTEGER NOT NULL
);

-- Pending emailed-code sign-ins: one row backs both the magic link and the
-- 6-digit code, both stored hashed, single-use and short-lived. `attempts`
-- caps code guesses; `ip_hash` backs per-source send rate limits.
CREATE TABLE pending_auth (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL,
  token_hash  TEXT NOT NULL,
  code_hash   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0,
  ip_hash     TEXT
);
CREATE UNIQUE INDEX pending_auth_token ON pending_auth (token_hash);
CREATE INDEX pending_auth_email ON pending_auth (email, created_at);
CREATE INDEX pending_auth_ip ON pending_auth (ip_hash, created_at);

------------------------------------------------------------------------------
-- Who may sign in, and who they are in the attribution
------------------------------------------------------------------------------

-- The sign-in allowlist: non-staff emails get the `gcs` scope only while
-- listed here (`scopesFor` checks it on every request). Edited at
-- /admin/db/allowed_emails.
CREATE TABLE allowed_emails (
  email TEXT PRIMARY KEY,             -- lowercased
  note  TEXT,                         -- who this is / where they're from
  who   TEXT NOT NULL,                -- admin who added the row
  ts    INTEGER NOT NULL
);

-- Append-only history of every write made through /api/db (any table).
CREATE TABLE admin_edits (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  action   TEXT NOT NULL,             -- 'insert' | 'update' | 'delete'
  who      TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  old_json TEXT,
  new_json TEXT
);
CREATE INDEX admin_edits_tbl ON admin_edits (tbl, ts);

-- Sign-in email → attribution user id (the identities.yaml spelling), so a
-- viewer's "my files" finds the bytes the scan attributes to them.
CREATE TABLE user_emails (
  email TEXT PRIMARY KEY,
  user  TEXT NOT NULL,
  who   TEXT NOT NULL,
  ts    INTEGER NOT NULL
);
CREATE INDEX user_emails_user ON user_emails (user);

-- Personal agent tokens: the grant currently serving as a user's `gcs`-scoped
-- bearer token (only its hash lives in `grants`; the raw token is shown once).
CREATE TABLE agent_tokens (
  email    TEXT PRIMARY KEY,
  grant_id TEXT NOT NULL,
  created  INTEGER NOT NULL
);

------------------------------------------------------------------------------
-- Keep / sweep decisions and ownership: the actions ledger
------------------------------------------------------------------------------

-- The ledger: one append-only row per user gesture, on the keep axis
-- (`set_keep`/`keep`: 'keep' | 'keep_last_ckpt' | 'sweep' | NULL) and/or the
-- owner axis. Expanded per axis into the prefix tables below, where the most
-- recent action covering a prefix wins.
CREATE TABLE actions (
  id        INTEGER PRIMARY KEY,
  actor     TEXT NOT NULL,
  ts        INTEGER NOT NULL,
  scan      TEXT NOT NULL,            -- the scan the gesture was made against
  pattern   TEXT NOT NULL,            -- gs://bucket/prefix/ (or a regex)
  set_owner INTEGER NOT NULL DEFAULT 0,
  owner     TEXT,
  set_keep  INTEGER NOT NULL DEFAULT 0,
  keep      TEXT,
  memo      TEXT,
  CHECK (set_owner OR set_keep)
);
CREATE INDEX idx_actions_actor ON actions (actor, ts);

-- Keep axis, expanded: every prefix an action's pattern matched, with the keep
-- state it set. `tombstoned` marks rows a later action re-painted.
CREATE TABLE keep_prefixes (
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  keep       TEXT,
  ts         INTEGER NOT NULL,
  tombstoned TEXT,
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_keep_prefix_ts ON keep_prefixes (prefix, ts DESC);

-- Owner axis, expanded, same shape.
CREATE TABLE owner_prefixes (
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  owner      TEXT,
  ts         INTEGER NOT NULL,
  tombstoned TEXT,
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_owner_prefix_ts ON owner_prefixes (prefix, ts DESC);

-- Exact keep / sweep / last-ckpt / undecided totals per (scan, ledger head):
-- the folded ledger priced against the path index, cached so the index reads
-- happen once per ledger change. `claims` is the per-user slice of `body`.
CREATE TABLE mark_totals (
  scan        TEXT NOT NULL,
  head        INTEGER NOT NULL,
  body        TEXT NOT NULL,          -- JSON manifest
  computed_ts INTEGER NOT NULL,
  ms          INTEGER,
  claims      TEXT,
  PRIMARY KEY (scan, head)
);

-- Pre-ledger marks, claims and their log: superseded by `actions` (their
-- contents were migrated into it) and kept read-only as history.
CREATE TABLE marks (
  prefix TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('keep', 'keep_last_ckpt', 'delete')),
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  note   TEXT
);
CREATE TABLE mark_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  prefix TEXT NOT NULL,
  action TEXT,
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  note   TEXT
);
CREATE INDEX mark_log_prefix ON mark_log (prefix, ts);
CREATE TABLE claims (
  prefix TEXT PRIMARY KEY,
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL
);

------------------------------------------------------------------------------
-- Path-index metadata (the parquet footer, decomposed so the edge reader can
-- plan a prefix query without parsing the whole footer)
------------------------------------------------------------------------------

-- One row per (scan, index variant): the parquet schema, the tier's byte
-- floor (NULL = floor-free), and the pointer to the generation currently
-- serving (`gen`, `dir`), written last so two generations can coexist while a
-- new one lands. Variants: 'path' (drill), 'user', 'team'.
CREATE TABLE index_schema (
  date        TEXT NOT NULL,
  variant     TEXT NOT NULL DEFAULT 'path',
  version     INTEGER NOT NULL,
  schema_json TEXT NOT NULL,
  floor_bytes INTEGER,
  gen         TEXT,
  dir         TEXT,
  PRIMARY KEY (date, variant)
);

-- One row per row group of each generation: the stats the reader prunes on
-- (depth, path and usr ranges, max bytes) and the row-group metadata itself.
CREATE TABLE index_row_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL,
  gen       TEXT NOT NULL,
  rg        INTEGER NOT NULL,
  d_min     INTEGER NOT NULL,
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,
  u_min     TEXT,
  u_max     TEXT,
  row_start INTEGER NOT NULL,
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL,
  PRIMARY KEY (date, variant, gen, rg)
);
CREATE INDEX idx_index_row_groups_depth ON index_row_groups (date, variant, gen, d_min, d_max);
CREATE INDEX idx_index_row_groups_user ON index_row_groups (date, variant, gen, u_min, u_max);

------------------------------------------------------------------------------
-- Deletion plans and executed sweeps
------------------------------------------------------------------------------

-- A deletion plan as a first-class object (several can be drafted at once,
-- each with its own dry → real lifecycle).
CREATE TABLE plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_by TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  closed_ts  INTEGER
);

-- One staging gesture: N prefixes trashed together under one memo.
CREATE TABLE stage_batches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  note       TEXT,
  created_by TEXT NOT NULL,
  created_ts INTEGER NOT NULL
);

-- The prefixes in a plan; `batch_id` links back to the gesture that staged them.
CREATE TABLE plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans(id),
  prefix   TEXT NOT NULL,
  note     TEXT,
  added_by TEXT NOT NULL,
  added_ts INTEGER NOT NULL,
  batch_id INTEGER REFERENCES stage_batches(id),
  PRIMARY KEY (plan_id, prefix)
);

-- Human approval of a sweep band before the executor may delete under it.
-- `mode` 'slice' deletes only directories attributed to the band's sweeper;
-- 'full' the whole band.
CREATE TABLE sweep_approvals (
  prefix TEXT PRIMARY KEY,
  scan   TEXT NOT NULL,
  head   INTEGER NOT NULL,
  note   TEXT,
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  mode   TEXT NOT NULL DEFAULT 'slice'
);

-- Executed sweeps: one row per executor invocation (dry or real), with what it
-- deleted, what drifted since the plan, the undo window, and the CAIOS purge
-- stage (always 'none' on GCS). Object-level detail lives in `log_dir`.
CREATE TABLE deletion_runs (
  run_id              TEXT PRIMARY KEY,
  plan                TEXT NOT NULL,
  scan                TEXT NOT NULL,
  head                INTEGER NOT NULL,
  exec_head           INTEGER NOT NULL,
  actor               TEXT NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('dry', 'real')),
  started_ts          INTEGER NOT NULL,
  finished_ts         INTEGER,
  deleted_bytes       INTEGER NOT NULL DEFAULT 0,
  deleted_objects     INTEGER NOT NULL DEFAULT 0,
  skipped_gone        INTEGER NOT NULL DEFAULT 0,
  skipped_overwritten INTEGER NOT NULL DEFAULT 0,
  drift_dirs          INTEGER NOT NULL DEFAULT 0,
  ledger_drift_dirs   INTEGER NOT NULL DEFAULT 0,
  undo_deadline       INTEGER,
  undo_state          TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  log_dir             TEXT NOT NULL,
  buckets             TEXT,           -- comma-separated bucket cut; NULL = all
  plan_id             INTEGER REFERENCES plans(id),
  purge_state         TEXT NOT NULL DEFAULT 'none' CHECK (purge_state IN ('none', 'pending', 'done'))
);

-- A run's deletions aggregated per covering band prefix — the queryable
-- per-path unit of deletion history.
CREATE TABLE deletion_bands (
  run_id            TEXT NOT NULL REFERENCES deletion_runs(run_id),
  prefix            TEXT NOT NULL,
  bytes             INTEGER NOT NULL,
  objects           INTEGER NOT NULL,
  gone              INTEGER NOT NULL DEFAULT 0,
  overwritten       INTEGER NOT NULL DEFAULT 0,
  drift_new_objects INTEGER NOT NULL DEFAULT 0,
  undone_objects    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);
CREATE INDEX idx_deletion_bands_prefix ON deletion_bands (prefix);
