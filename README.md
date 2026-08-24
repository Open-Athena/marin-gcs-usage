# marin-gcs-usage

Storage-usage attribution and cleanup for the Marin GCS buckets: **who is using
what**, and a **mark & sweep** workflow to reclaim space. Browse it at
**[gcs.oa.dev]** (Open-Athena-gated).

The identity map (`marin/src/gcs_usage/identities.yaml`: handles, teams, login
aliases) is curated in-repo; it maps already-public GitHub / W&B handles to a
team bucket (`oa` / `stanford` / `communal`) and carries no emails or private
contact info.

## Mark & sweep

Storage across the `marin-*` buckets is reviewed by **marking prefixes to keep**
— everything left unmarked is **swept (deleted) after the cleanup deadline**.
Mark from the web UI at [gcs.oa.dev], or non-interactively:

- `gcs-usage mark` / `status` / `todo` — the CLI (bulk-mark, check a prefix's
  effective fate, list the undecided backlog).
- `GET /api/resolve`, `GET /api/todo`, `POST /api/actions` — the HTTP API.

**[AGENTS.md](AGENTS.md)** documents the token, CLI, and API for driving this
autonomously (e.g. pointing an agent at your team's prefixes). Marking only
records a decision in the ledger — nothing is deleted at mark time, and marks
are reversible until the sweep.

## Attribution pipeline

Attribution parquets (`prefix → user/team` rows) come from two builders:

```bash
# Path + record signals from the listing itself:
gcs-usage build -l 'gs://<bucket>/<scan>/objects/*.parquet' -o tmp/attribution.parquet
gcs-usage build -l <listing> -o <out> -R   # path signals only (no GETs)

# W&B signals (the bulk of coverage):
gcs-usage wandb-mine -e marin-community -o tmp/wandb-runs.parquet   # full API mine (time-bisected; -E/-s/-u for parallel range workers)
gcs-usage executor-mine -l <listing> -o tmp/executor-infos.parquet  # .executor_info sidecar GETs
gcs-usage wandb-attr -r tmp/wandb-runs.parquet -x tmp/executor-infos.parquet -l <listing> -o tmp/attribution-wandb.parquet
```

Reporting and the site consume any number of attribution parquets (`-a`, repeatable):

```bash
gcs-usage report -l <listing> -a <attr...>            # per-user/team bytes + coverage; -u <user> prints their claim list
gcs-usage gaps -l <listing> -a <attr...> -d 2         # largest unattributed prefixes (curation queue)
gcs-usage webdata -l <listing> -d <asof> -a <attr...> # site snapshot → site/public/data/<asof>/ (+ scans.json index)
gcs-usage rules -o site/public/data/rules.json        # validate identities.yaml; export rules for the site
```

Signals, roughly best-first (deepest-prefix-wins at join time):

1. W&B run-config writer paths (`base_path` & friends — checkpoint/output dirs the trainer wrote)
2. W&B run-name ↔ dir joins under `checkpoints/`/`grug/`
3. `.executor_info` → W&B run joins
4. `users/<seg>/` path prefixes ([marin#6790] namespacing)
5. `.artifact.json` sidecars → `provenance.built_by`
6. manual `prefix_owners` for big shared trees (datasets, sweep namespaces)

Users/teams are re-resolved against the *current* `identities.yaml` at load
time, so alias/team curation takes effect without rebuilding parquets. Unknown
spellings resolve to their own sanitized segment with team `unknown` and are
listed on stderr — curate them into `identities.yaml` (`gcs-usage rules`
validates it).

Listing-scale runs (34M+ dirs) belong on a work node, not a laptop.

## Access ([gcs.oa.dev])

The viz site is gated by [Cloudflare Access][cf-access] (app "GCS usage", Open Athena CF account). The allow policy is:

- any `@openathena.ai` email (Google SSO or one-time email PIN), plus
- a whitelist of external emails — currently Percy Liang: `psl@stanford.edu`, `percyliang@gmail.com` (one-time email PIN; Google SSO is restricted to the openathena.ai org by the OAuth client's consent config)

To add/remove whitelisted emails: CF dashboard → Zero Trust → Access → Applications → "GCS usage" policy (or ask Ryan). Update this list in lockstep so the policy stays reviewable here.

## Repo layout

Monorepo — a shared engine plus the Marin-specific app and site:

- **`marin/`** — the `marin-gcs-usage` package (the `gcs-usage` CLI: attribution
  builders, reporting, the mark & sweep ledger client, access-log ingest).
- **`src/disk_tree/`** — the [disk-tree] engine (indexing, tree aggregation,
  storage backends) this repo is built on; installed as the root `disk-tree`
  package.
- **`site/`** — the [gcs.oa.dev] web app: a Cloudflare Pages SPA (treemap +
  browse + mark UI) with Pages Functions serving `/data` and `/api/*`.

## Development

The `gcs-usage` CLI lives in `marin/`:

```bash
cd marin
uv sync
uv run pytest
```

Two marin contracts are deliberately mirrored (not imported) to keep this repo
standalone; if either changes upstream, update in lockstep:

- `marin/src/gcs_usage/usernames.py` — `sanitize_username` rules, mirror of
  `rigging.provenance.username_segment`
- `marin/src/gcs_usage/records.py` — the `.artifact.json` shape
  (`marin.execution.artifact.ArtifactRecord`), of which only
  `provenance.built_by` is read

For the web app (`site/`), see [`site/`](site) — `./dev` runs the full local
stack (Vite UI + `wrangler pages dev` for the Functions).

[gcs.oa.dev]: https://gcs.oa.dev
[cf-access]: https://developers.cloudflare.com/cloudflare-one/applications/
[marin]: https://github.com/marin-community/marin
[marin#6790]: https://github.com/marin-community/marin/issues/6790
[disk-tree]: https://github.com/runsascoded/disk-tree
