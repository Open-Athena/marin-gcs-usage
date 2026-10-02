-- Full-info share cards (specs/dogi.md): each mint of a per-view token, for
-- audit and revocation. A token (13 chars: expiry day + 64-bit tag) is
-- deterministic per (view, expiry day), so two people minting one view on one
-- day get the same token and two rows. A token is honoured only while a row
-- for it exists and none is revoked. New table, no references in or out.
CREATE TABLE og_tokens (
  id         INTEGER PRIMARY KEY,
  token      TEXT NOT NULL,
  kind       TEXT NOT NULL,     -- the card kind (`map`, `staged`, …)
  view       TEXT NOT NULL,     -- the canonical view params it covers
  page       TEXT NOT NULL,     -- the page URL path + view, without `og=`
  minted_by  TEXT NOT NULL,     -- the minter's email (or `slack:` for server posts)
  minted_ts  INTEGER NOT NULL,
  exp_day    INTEGER NOT NULL,  -- days since 2026-01-01; valid through that day (UTC)
  revoked_by TEXT,
  revoked_ts INTEGER
);
CREATE INDEX idx_og_tokens_token ON og_tokens (token);
CREATE INDEX idx_og_tokens_minted ON og_tokens (minted_ts DESC);
