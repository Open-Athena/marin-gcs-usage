# Marin GCS usage: ownership & staged deletion, an API for agents

This repo powers <https://gcs.oa.dev>: **who owns what** across the `marin-*` GCS buckets, and a **staged-deletion** workflow to reclaim space. Anyone signed in can **stage** prefixes for deletion (the trash gesture in the UI, or the API below); admins review the shared plan at [`/staged`](https://gcs.oa.dev/staged) and dispatch the executor. **Nothing is deleted at stage time**: staging is a proposal, reversible until an admin runs it. Ownership is the other axis: anyone signed in can **assign** a prefix to anyone (themselves included), or clear an assignment; the ledger records who did what.

This file is the whole guide. Point your agent at it (in the [GitHub repo][API.md], or served unauthenticated at <https://gcs.oa.dev/llms.txt>) and ask it to "find my stuff, stage what's deletable, assign what I know the owner of". Copy-pasteable versions of each step are under [Recipes](#3-recipes).

(The earlier opt-out "mark & sweep" model, keep/sweep marks plus a deadline, was retired 2026-09-28; `dt-cloud mark|status|todo` and `/api/marks*`, `/api/resolve`, `/api/todo` are gone.)

## 1. Get a token

Every write is authenticated as **you** by a personal bearer token.

- **Browser:** sign in at <https://gcs.oa.dev> with your `@openathena.ai` (or whitelisted) email, then reveal/mint your token from the user menu (top-right).
- **API:** `POST /api/token` from a signed-in browser session mints (or rotates) it; the raw token is shown **once**. `GET /api/token` reports status (never the token); `DELETE /api/token` revokes it.

The token carries the `gcs` and `gcs:assign` scopes (least privilege: it can read, stage and assign, not administer). Revoking it is instant. Share links (guest access) can't assign.

```bash
export GCS_USAGE_TOKEN=…                  # the token you copied
export GCS_USAGE_URL=https://gcs.oa.dev   # the site
```

Prefixes are always `gs://marin-<bucket>/<dir>/…/`: **directory prefixes only** (trailing slash), in one of the six `marin-*` buckets. The read APIs' `path=` is the same prefix without the scheme or trailing slash (`marin-us-east5/checkpoints`; empty = all buckets).

**You are `me`.** Your owner id is what `GET /api/me` reports as `user`; `user=me` (`/api/estate`), `lens=user:me` (`/api/subtree`, `/api/diff`, `/api/series`) and `owner: '@me'` (`POST /api/actions`) all resolve to it.

## 2. HTTP API

All under `$GCS_USAGE_URL`. Reads are open to any signed-in viewer; writes need `Authorization: Bearer $GCS_USAGE_TOKEN`. Scans are dated `YYYY-MM-DD`; `GET /data/scans.json` lists them, newest first.

### Identity

| Method & path | Purpose |
|---|---|
| `GET /api/me` | `{ email, user, scopes, admin, via }`: `user` is your canonical owner id (null for a guest link without an email). |

### Staged deletion

| Method & path | Purpose |
|---|---|
| `POST /api/plans/stage` | Stage prefixes: `{ prefixes: [...], note? }` → `201 { plan_id, batch_id, staged, covered, absorbed }`. A prefix under an already-staged ancestor comes back as `covered` (nothing to add); staging an ancestor `absorbs` its staged descendants (the plan keeps the ancestor). Any signed-in stager. |
| `GET /api/plans/staged` | The shared open plan, grouped the way `/staged` shows it: `{ plan, items, batches, emptied, runs }`, items with `prefix, note, added_by, added_ts, batch_id`. |
| `DELETE /api/plans/<id>/items` | Unstage: `{ prefixes: [...] }`. Stagers may remove their own items; admins any. |
| `GET /api/plans`, `GET /api/plans/<id>` | Plans and one plan's items / batches / runs. |
| `POST /api/plans`, `PATCH /api/plans/<id>`, `POST /api/plans/<id>/items` | Admin curation (create, close `{ state: 'closed' }`, add items). |
| `POST /api/sweep/dispatch` | Admin: run the executor over a plan (`{ plan_id, mode: 'dry' \| 'real', date, buckets? }` → a GCP Batch job); `GET /api/sweep/jobs` lists runs, `POST /api/sweep/stop` stops one. |

### Deletion runs (what was deleted, and asking for an undo)

Every run, dry or real, is readable by any signed-in viewer; only admins dispatch, stop or undo one.

| Method & path | Purpose |
|---|---|
| `GET /api/plans/staged` | The open plan's items, plus `runs`: every deletion run (dry and real), with `run_id`, `mode`, totals (`deleted_bytes`, `deleted_objects`) and its `undo_deadline`. |
| `GET /api/sweep/jobs` | The executor's Batch jobs (runs and undos), live state included. |
| `GET /api/plans/run?id=<run_id>` | One run: `{ run, bands }`, one band per deleted prefix (`prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects`). Run ids hold a `/`: URL-encode them. |
| `GET /api/plans/prefix-history?id=<plan_id>` | Each staged prefix of a plan → the real run(s) that deleted it. |
| `GET /v1/files/list?prefix=sweep/runs/<run_id>/` | A run's directory (plan, per-bucket manifests of the exact objects + generations deleted); fetch a file with `/v1/files/get?path=…`. |

Deleted objects stay recoverable for 7 days from their deletion (each run's `undo_deadline`, epoch seconds). **There is deliberately no undo API.** To get something back, send an admin the run id and the prefixes (a reply on the run's Slack/Discord thread, or a DM); an agent can assemble that list with the recipe below. `/runs/<run_id>` is the same run as a page.

### Ownership

| Method & path | Purpose |
|---|---|
| `GET /api/owners?date=<scan>` | Per-user owned bytes + storage-class mix for a scan (what `/users` ranks): `{ scan, head, bytes, objects, users: { <id>: { b, mix } } }`. |
| `GET /api/estate?date=<scan>&user=<id \| me>` | One user's estate: `{ user, date, head, bytes, objects, mix, assignments }`; `assignments` are their live assignments, each `{ prefix, ts, bytes, objects }`. |
| `GET /api/assignments?date=<scan>` | The assigner × assignee matrix (who assigned what to whom, in bytes). |
| `GET /api/actions` | The live ownership ledger: `{ owners: [...] }`, every expanded prefix (`prefix, owner, ts, who, memo, action_id`). `?log=1` is every action, newest first, with its status. |
| `POST /api/actions` | Assign: one action or an array (≤500) of `{ pattern, owner, memo?, scan? }`. `owner` is a user id (as `/api/owners` keys them), `'@me'` resolves to you, `null` clears. Prefix patterns only. The newest assignment on a prefix or any ancestor wins. |

### Data

| Method & path | Purpose |
|---|---|
| `GET /data/scans.json`, `/data/<scan>/{tree,age,meta}.json`, `/data/rules.json` | The published per-scan artifacts the UI renders (scans.json = the scan ids, newest first; tree = size-floored rollup; meta = totals + per-user/class bytes). |
| `GET /api/subtree?date=&path=&w=&h=` | Pixel-budget subtree of any path: `{ tree, … }`, UI-shaped nodes `{ n, k, b, o, d, a, us, cb, c }` (`n` = the name segment, `b` bytes, `o` objects, `us` = top owners `[[id, bytes], …]`, `c` children; unowned = `b` − Σ `us`), folded to what a w×h canvas can draw (a fold is an `(other)` node with `f`). Scopes: `lens=user:<id \| me>` (one owner's bytes), bare `o` (the unowned pool), `o=*` (the owned pool), `depth=N` (levels below `path`). |
| `GET /api/diff?from=&to=&path=` | What changed under a path between two scans; same scopes. |
| `GET /api/series?path=` | Bytes under a path per scan; same scopes. |
| `GET/HEAD /api/path-index?date=` | The **floor-free** path index behind `/api/subtree`, as raw parquet with HTTP Range support: bring your own query engine (see below). One row per rolled-up path × owner slice: `(path, depth, usr, b, o, wts, wb, c2, c3, c4, a)`, sorted `(depth, path)`; `usr` NULL = unowned. |
| `GET /v1/files/<path>` | Raw scan-store proxy (range-supporting) over `listing/` + `snapshots/`: the per-object listing parquets, `dir-cache/`, the index tiers (`index/<gen>/path-index*.parquet`; older scans have them at `listing/<date>/` directly), and published snapshot JSONs, addressed by bucket path. |

Human-facing pages, same data: [`/staged`](https://gcs.oa.dev/staged) is the deletion console, [`/users`](https://gcs.oa.dev/users) the per-user estate ranking, `/user/<id>` one user's estate, `/assignments` the assignment heatmap, and `/runs/<id>` one deletion run (progress, logs, what it deleted, recovery).

```sql
-- DuckDB, straight against prod (httpfs sends HEAD + range GETs, so a
-- depth/path predicate only fetches the row groups it needs):
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
SELECT path, sum(b) AS bytes FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
WHERE depth = 2 GROUP BY path ORDER BY bytes DESC LIMIT 20;
```

## 3. Recipes

`bash` + `curl` + `jq`. Run the setup once; every recipe after it assumes it.

```bash
gu() { curl -sS --fail-with-body -H "Authorization: Bearer $GCS_USAGE_TOKEN" "$@"; }
DATE=$(gu "$GCS_USAGE_URL/data/scans.json" | jq -r '.[0]')   # the latest scan
# A subtree's nodes as rows with full paths (folds dropped), biggest first: jq -f rows.jq
cat > rows.jq <<'EOF'
def rows($p): (.c // [])[] | select(.f == null)
  | (if $p == "" then .n else "\($p)/\(.n)" end) as $q
  | ({ path: $q, gb: (.b / 1e9 * 10 | round / 10), objects: .o, owners: (.us // []) }, rows($q));
[.path as $p | .tree | rows($p)] | sort_by(-.gb) | .[:30][]
EOF
```

**Who am I / my owner id**

```bash
gu "$GCS_USAGE_URL/api/me" | jq '{ email, user, scopes }'
```

**My estate, and my biggest paths**

```bash
# Owned bytes + class mix, and the prefixes assigned to you, biggest first
gu "$GCS_USAGE_URL/api/estate?date=$DATE&user=me" \
  | jq '{ user, bytes, objects, mix, assignments: (.assignments | sort_by(-.bytes)) }'
# Your bytes (assigned or inferred) two levels deep under a path; drill by changing `path`
gu "$GCS_USAGE_URL/api/subtree?date=$DATE&path=&lens=user:me&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**The biggest unowned paths under a prefix**

```bash
# bare `o` = the unowned pool (`o=*` = the owned pool)
gu "$GCS_USAGE_URL/api/subtree?date=$DATE&path=marin-us-east5/checkpoints&o&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**Stage prefixes for deletion, with a note**

```bash
gu -X POST "$GCS_USAGE_URL/api/plans/stage" -H 'Content-Type: application/json' \
  -d '{"prefixes":["gs://marin-us-east5/scratch/old-run/","gs://marin-us-central2/tmp/2025/"],"note":"superseded by run 42"}' \
  | jq '{ plan_id, staged, covered, absorbed }'
```

**Unstage**

```bash
PLAN=$(gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.plan.id')
gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.items[] | [.prefix, .added_by, .note] | @tsv'   # what's staged
gu -X DELETE "$GCS_USAGE_URL/api/plans/$PLAN/items" -H 'Content-Type: application/json' \
  -d '{"prefixes":["gs://marin-us-east5/scratch/old-run/"]}'
```

**Assign: to me, to someone, or clear**

```bash
assign() { gu -X POST "$GCS_USAGE_URL/api/actions" -H 'Content-Type: application/json' -d "$1"; }
assign '{"pattern":"gs://marin-us-east5/checkpoints/my-run/","owner":"@me","memo":"my sweep"}'
gu "$GCS_USAGE_URL/api/owners?date=$DATE" | jq -r '.users | keys[]'   # the user ids
assign '{"pattern":"gs://marin-us-east5/checkpoints/their-run/","owner":"<id>","memo":"their W&B run"}'
assign '{"pattern":"gs://marin-us-east5/checkpoints/shared/","owner":null,"memo":"shared, no single owner"}'
```

The newest assignment on a prefix or any ancestor wins, so assigning (or clearing) an ancestor overrides older assignments below it. Assign the deepest prefix that names what you mean.

**What did run X delete?**

```bash
gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | [.run_id, .deleted_bytes, ((.undo_deadline | numbers | todate) // "-")] | @tsv'
gu -G "$GCS_USAGE_URL/api/plans/run" --data-urlencode "id=<run_id>" \
  | jq -r '.bands[] | [.prefix, .bytes, .objects, .undone_objects] | @tsv'
```

**Is my prefix in a deleted run?**

```bash
P=gs://marin-us-east5/checkpoints/my-run/
for r in $(gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | .run_id'); do
  gu -G "$GCS_USAGE_URL/api/plans/run" --data-urlencode "id=$r" | jq -r --arg p "$P" \
    '.run as $run | .bands[] | .prefix as $b | select(($b | startswith($p)) or ($p | startswith($b)))
     | [$run.run_id, $b, .bytes, (($run.undo_deadline | numbers | todate) // "-")] | @tsv'
done
```

**Asking for an undo.** There is no API for it, on purpose: an undo is an admin's call. Message an admin (a reply on the run's Slack/Discord thread, or a DM) with the run id, the prefixes you want back (the recipe above lists them), and why, before the run's `undo_deadline`.

## Notes for agents

- **Staging is reversible until dispatch.** Unstage any time (`DELETE /api/plans/<id>/items`); only an admin's `real` run deletes, and runs are undoable for a window afterwards (see `/staged`).
- **Browse first:** `/api/subtree` (or the parquet index) tells you what a prefix holds and who owns it; stage the deepest prefixes that name what you mean, not a fat ancestor: an ancestor absorbs everything under it.
- **Explain yourself:** the `note` on a stage batch is what the reviewing admin reads on `/staged`; the `memo` on an assignment is what the ledger shows.
- There is no client CLI for staging; the surviving `dt-cloud` verbs are the pipeline's and the executor's (`healthcheck`, `sweep manifest --plan`, …).

[API.md]: https://github.com/Open-Athena/marin-gcs-usage/blob/gcs/API.md
