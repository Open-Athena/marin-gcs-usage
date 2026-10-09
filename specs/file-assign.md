# Single files: assignable and stageable

Status: in progress on branch `file-assign` (off `cloud`).

Today owner assignments and plan items (and so the sweep) only take folder prefixes. A lone matching file — e.g. `ACS_(Part_1)_…_chunk003.mp3` in a folder that also holds non-matching files — can't be acted on: the filtered bulk bar lists it but never sends it, and the unfiltered table's trash button on a file row stages `key/`, which deletes nothing.

## Design: every item carries an explicit kind

An action, an owner row and a plan item each carry `kind`:

- **`prefix`** (folder): `<scheme><bucket>/<path>/` — everything under it. Stored with `kind` NULL (every pre-existing row), so old rows read as prefixes unchanged.
- **`object`** (exact): `<scheme><bucket>/<key>` — that one key, nothing else. Stored with `kind = 'object'`.

The kind is never inferred from the string. Validation pins the two shapes apart as a second line of defence: a prefix is canonicalized to end in `/`; an object key must not end in `/` (folder-placeholder objects are not exact-assignable or stageable) and must not be a bucket root. So the stored strings of the two kinds never collide, but every reader still takes the kind from the column / the wire field.

### Ledger (`/api/actions`, gcs lineage)

- `POST /api/actions` takes `kind: 'prefix' | 'object'` (absent = `prefix`, the old wire). An object action's `pattern` is the exact key URI.
- `actions.kind` and `owner_prefixes.kind` record it (NULL = prefix). `GET /api/actions` returns `kind` on every row (`'prefix'` for NULL).
- **Resolution.** An exact action owns exactly its key. On a path's ancestor-or-self chain, an exact action sits at the leaf: it is more specific than any folder, but recency still decides — the newest action on the chain wins, exactly as between folders (`ownership-ledger-nesting`):
  - folder A (older) + object K under A (newer) → K is the object's owner; the rest of A stays A's.
  - object K (older) + folder A over it (newer) → A repaints K (a newer broad action is a bulk override, as today).
  - object K never covers anything else: not `K.bak`, not `K/…` (a folder that happens to share K's name).
- The folds that implement this: the client `ownerIndex.assignmentOf(uri, kind)`, the server `computeOwners` (bands: an object node is a leaf band, its parent folder's band excludes it) and `ownerLens` / `poolLens` (by index path; an object is a leaf). The audit log's `superseded` / `overridden` status: only a folder row can override a deeper row; an object row is superseded only by a newer row on the identical object.

### Staging and sweep

- `POST /api/plans/stage` takes `objects: string[]` beside `prefixes: string[]`. `plan_items.kind` records the kind (NULL = prefix).
- No-nesting rule (`planStaging`): an object under a staged prefix is `covered`; staging a prefix absorbs the staged objects under it; an object never covers or absorbs anything (`clip.mp3` vs `clip.mp3.bak` are independent).
- Dispatch snapshots: the cw `plan.json` gets `objects` (relative keys) beside `sweep` (relative prefixes); the gcs `plan.json` gets `objects` (canonical `gs://` keys) beside `sweep`. `as_of` is keyed by either. An old executor ignores `objects` (an objects-only plan fails its non-empty-`sweep` check): the safe direction.
- `planDigest` hashes prefixes as before and objects as `=<key>` lines, so a prefix-only plan's digest is unchanged and a kind change changes it.
- Manifest (gcs `sweep_manifest.py`, cw `sweep.py`): an exact item matches `name == key` only — through row-group pruning, the eligible mask, the `as_of` hold (generation / created pinning) and the minimal-band reduction (an exact key under a staged prefix is dropped as redundant). `plan-summary.json` lists exact items as `approved_objects` beside `approved`.
- Execute (`sweep_exec.py`): deletes are manifest-driven and generation-matched already; for exact items, (1) per-band accounting maps a deleted row to its exact item (so `deletion_bands.prefix` = the object key and `unstageDeleted` takes the item out), (2) a new sibling in an exact-only directory is not drift (drift gates prefix-covered directories only), (3) the listing root of an exact-only directory is that directory, not its top-level segment.
- Undo is per-object from the deletion log (generation-pinned), so it restores exactly what was deleted; its per-band accounting is exact-aware like execute's.

### UI

- Filtered bulk bar: a `file` cover item is sent as an object item (assign: `kind: 'object'`; stage: `objects`). Counts read "N folders, M files". The "lone file can't be acted on" caveat is gone; whole buckets and unchecked matches keep theirs.
- Review panel copy: a line is a folder or a single file; either can be sent.
- Unfiltered table trash button on a file row stages the file as an object item (was `key/`).

### Migrations (additive only; apply needs Ryan's go)

- cw (`site/migrations/cw/0017_plan_items_kind.sql`, on `cloud`): `ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object')`.
- gcs (the gcs session adds it under `site/migrations/gcs/`, next number after `0040`):
  ```sql
  ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ALTER TABLE actions ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ALTER TABLE owner_prefixes ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ```
  No table rebuild (`owner_prefixes.action_id REFERENCES actions(id)`; see `d1-enforces-foreign-keys`). The code reads the column unconditionally, so the migration lands before the deploy.

### Back-compat

Every existing row is NULL-kind = prefix; every existing wire body has no `kind` / `objects` = prefixes only. Prefix semantics (coverage, nesting, digest, manifest, drift, roots) are unchanged, and tests pin that.
