-- Exact (file) items: a plan item, a ledger action and an owner prefix may name
-- one object (`kind = 'object'`) instead of a directory prefix (NULL, the
-- default: every existing row). Additive only — no table rebuild (D1 enforces
-- FKs; `actions` is referenced).
ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
ALTER TABLE actions ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
ALTER TABLE owner_prefixes ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
