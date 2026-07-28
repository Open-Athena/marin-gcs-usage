# marin-gcs-usage

Per-user attribution and reporting for Marin GCS storage: "who is using what."

Private by design — the identity map (`src/gcs_usage/identities.yaml`: real
names, teams, login aliases) and per-user usage reports stay out of the public
[marin] repo, matching the privacy stance of marin's egress report.

See [specs/storage-cost-attribution.md](specs/storage-cost-attribution.md) for
the full plan (attribution signals, join layer, weekly report integration,
OA-gated webapp, [disk-tree] drill-down).

## Usage

```bash
# Build the sparse dir → user attribution table from a marin scan_gcs listing.
gcs-usage build -l 'gs://<bucket>/<scan>/objects/*.parquet' -o dir_attribution.parquet

# Path signals only (no GETs): users/<seg>/ prefixes + manual prefix_owners.
gcs-usage build -l <listing> -o <out> -R
```

Signals, best-first (deepest-prefix-wins at join time):

1. `users/<seg>/` path prefixes ([marin#6790] namespacing)
2. `.artifact.json` sidecars → `provenance.built_by` (targeted GETs at paths
   the listing already names; legacy `.executor_info` carries no user and is
   skipped)
3. manual `prefix_owners` for big shared dirs (datasets, ferry checkpoints)

Unknown spellings resolve to their own sanitized segment with team `unknown`
and are listed on stderr — curate them into `identities.yaml`.

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
