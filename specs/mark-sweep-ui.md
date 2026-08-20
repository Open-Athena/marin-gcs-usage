# Mark & Sweep UI

Human-in-the-loop GCS cleanup: a marking interface on gcs.oa.dev where every marin user reviews their files before a deadline, after which unmarked data is swept. Committed publicly in [Percy's internal-discuss post] (2026-08-18): **reveal Monday 2026-08-24, marking through EOD Friday 2026-08-28, then unmarked files are deleted permanently.** Scoped in the Ryan/Percy 1:1 the same day (Granola notes).

n.b. the 1:1 notes say "Friday Aug 29", but Aug 29 is a Saturday; the Discord post's **Friday Aug 28** is the public commitment and governs.

## Requirements (from the 1:1 + Discord post)

- **Mark states per prefix**: `delete` (the default — absence of a mark), `keep-last-checkpoint`, `keep-all`. Editable until the deadline.
- **"My files" first**: each user primarily reviews prefixes attributed to *them* (the attribution rules we already run). They can also mark communal or unclaimed prefixes.
- **Lost & found**: unattributed prefixes (~510 TB, suspected mostly Calvin-swarm output) are claimable; unclaimed defaults to deletion, surfaced **least-recently-touched first** (needs the access-log atime plane — `specs/access-logs-and-cost.md`; fallback ordering: created-date `d` until atime lands).
- **Bulk ergonomics are the whole ballgame**: select-all + unselect-exceptions (mark a parent `keep-all`, carve out `delete` children). If marking is slow, people keep everything and the sweep recovers nothing.
- **Communal datasets** (DCLM, Dolma, raw/tokenized): default delete-if-untouched; Rav + Will sign off those specifically.
- Scope: the six `marin-*` GCS buckets (3.6 PB / 594M objects). CoreWeave is out of scope (Rav's manual sprint is already running there).

## Architecture

Extend this repo's site (same deploy, new route `/mark`) rather than a separate app: the treemap browser, attribution join, snapshot data plane, and CF Access gate all already exist here.

### Identity — adopt `@open-athena/auth`, Tier 2

Every gcs.oa.dev viewer is already authenticated by CF Access (OA domain + the Stanford whitelist), so v1 marking identity is **the Access email** — no new login step for anyone who can already see the dashboard. `$oa/auth` adoption (its `specs/adoption.md` already names us):

- Swap `site/src/AuthGate.tsx` (47 lines) for the package's `<AuthGate source={{kind:'edge'}}>` + `devIdentity` (shipped upstream for exactly our dev-mode case).
- Add the Tier-2 backend for the marks API: D1 database + the package migrations, gate in Pages Functions. Buys us the **audit log** (who marked what, when — we want that trail when the sweep is questioned later) and **named nonce-links** (mint-time `first`/`last`/`avatar` landed upstream 2026-08-19) for anyone outside the Access whitelist.
- Caution: link/session lifecycle semantics (disable-vs-revoke, sessions table) are being reworked upstream this week — consume via the `dist`-branch SHA (`pds gh auth`), don't vendor.

### Marks store (D1)

```sql
CREATE TABLE marks (
  prefix    TEXT NOT NULL,            -- gs://bucket/path/, deepest-mark-wins
  action    TEXT NOT NULL,            -- 'keep' | 'keep_last_ckpt' | 'delete'
  who       TEXT NOT NULL,            -- Access email (or auth grant sub)
  ts        INTEGER NOT NULL,
  note      TEXT,
  PRIMARY KEY (prefix)
);
CREATE TABLE mark_log (…);            -- append-only history of every change
CREATE TABLE claims (prefix TEXT PRIMARY KEY, who TEXT, ts INTEGER);  -- lost & found
```

Only non-default marks are stored (`delete` = absence), so the table stays small (thousands of rows, not millions). **Resolution = deepest-mark-wins over prefixes**, the same semantics as attribution rules — one mental model, one resolver.

### API (Pages Functions)

- `GET /api/marks` — all marks + claims (small; the UI overlays them on the tree client-side)
- `PUT /api/marks` — upsert `{prefix, action, note?}`; `who`/`ts` from the session; every write appends to `mark_log`
- `POST /api/claims` — claim a lost-and-found prefix (then mark it like your own)

### UI

- **`/mark` route**: the existing treemap + directory drill, with a mark-state overlay (color-edge or badge per cell: kept / keep-last-ckpt / doomed-by-default) and mark buttons on the pinned-node panel.
- **Tabs**: *My files* (prefixes attributed to the viewer — default landing), *Lost & found* (unattributed, least-recently-touched first), *Communal* (Rav/Will), *Everything*.
- **Progress + countdown header**: "X TB of your Y TB reviewed · sweep in N days" — the nag is part of the product.
- **Depth**: marking needs to reach prefixes below the current webdata depth cap (the "19 TB flat" recursion complaint). Two-part fix: per-subtree JSON slices for drill-past-the-cap (the lazy-subtree data half DT already ships the widget side of), plus a path input for marking an arbitrary typed prefix.
- After the deadline: read-only freeze; the marks table is the sweep's input.

### Sweep (post-deadline, not in the UI)

1. Resolve: DuckDB join of the listing parquet against resolved marks (deepest-wins) → per-object verdicts → **sweep manifest** (per-prefix rollup, sizes, owners).
2. `keep_last_ckpt` resolution: within the marked prefix, keep the max step per checkpoint family (`global_step_(\d+)` / `step[-_]?(\d+)`), delete lower steps. Exact pattern list needs a survey of real checkpoint layouts before the deadline.
3. Review gate: manifest posted for sign-off (Percy + Rav/Will for communal) before any delete runs.
4. Execute with the batched-delete tooling from the grug cleanup (dry-run first, per-prefix logging, resumable), bucket by bucket.

## Milestones (deadline: live Monday 2026-08-24)

- **Wed 8/20**: D1 + auth Tier-2 adoption branch; marks API round-tripping; mark buttons on the tree (CIC).
- **Thu 8/21**: My-files / Lost-and-found / Communal tabs; deepest-wins overlay rendering; bulk mark + exceptions.
- **Fri 8/22**: countdown/progress header; depth fix (per-subtree slices or path-input fallback); polish pass with Yael/Percy feedback.
- **Weekend**: seed data QA (attribution recall — Calvin fix landed 8/19), dry-run walkthrough, announcement draft for the nag channel.
- **Mon 8/24**: reveal (Percy's post already promises it).
- **Post-8/28**: manifest export, sign-off, sweep runbook execution.

## Open questions

- `keep_last_ckpt` family patterns — enumerate real layouts (`checkpoints/<run>/global_step_N/`, `step-N/`, HF `checkpoint-N/`) before promising the semantics.
- Do lost-and-found claims need admin confirmation, or is claim==mark enough?
- Mark granularity floor: prefix-only (proposed) vs per-object (rejected: 594M rows).
- Does anyone outside the Access whitelist need to mark (→ mint nonce-links), or is the current allowlist the full marker set?
- Where does the sweep manifest live for sign-off — GH issue, Slack thread, or a page in the UI?

[Percy's internal-discuss post]: https://discord.com/channels/1354881461060243556/1412294350645493840/1539403061129257092
