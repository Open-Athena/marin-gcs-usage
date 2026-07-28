# marin-gcs-usage

Per-user attribution and reporting for Marin GCS storage: "who is using what."

Private by design — the identity map (`src/gcs_usage/identities.yaml`: real
names, teams, login aliases) and per-user usage reports stay out of the public
[marin] repo, matching the privacy stance of marin's egress report.

See [specs/storage-cost-attribution.md](specs/storage-cost-attribution.md) for
the full plan (attribution signals, join layer, weekly report integration,
OA-gated webapp, [disk-tree] drill-down).

## Usage

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

Listing-scale runs (34M+ dirs) belong on a work node, not a laptop — see the
weekly-refresh runbook in [specs/storage-cost-attribution.md](specs/storage-cost-attribution.md).

## Access ([gcs.oa.dev])

The viz site is gated by [Cloudflare Access][cf-access] (app "GCS usage", Open Athena CF account). The allow policy is:

- any `@openathena.ai` email (Google SSO or one-time email PIN), plus
- a whitelist of external emails — currently Percy Liang: `psl@stanford.edu`, `percyliang@gmail.com` (one-time email PIN; Google SSO is restricted to the openathena.ai org by the OAuth client's consent config)

To add/remove whitelisted emails: CF dashboard → Zero Trust → Access → Applications → "GCS usage" policy (or ask Ryan). Update this list in lockstep so the policy stays reviewable here.

## Development

```bash
uv sync
uv run pytest
```

Two marin contracts are deliberately mirrored (not imported) to keep this repo
standalone; if either changes upstream, update in lockstep:

- `src/gcs_usage/usernames.py` — `sanitize_username` rules, mirror of
  `rigging.provenance.username_segment`
- `src/gcs_usage/records.py` — the `.artifact.json` shape
  (`marin.execution.artifact.ArtifactRecord`), of which only
  `provenance.built_by` is read

[gcs.oa.dev]: https://gcs.oa.dev
[cf-access]: https://developers.cloudflare.com/cloudflare-one/applications/
[marin]: https://github.com/marin-community/marin
[marin#6790]: https://github.com/marin-community/marin/issues/6790
[disk-tree]: https://github.com/runsascoded/disk-tree
