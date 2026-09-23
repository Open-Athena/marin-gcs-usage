# Reusable multi-cloud usage stack: disk-tree engine + fork-pattern app

Rewritten 2026-08-14 after reviewing disk-tree's state (all items from its 8/3 engine spec are
**landed**: import + sharded gcs/s3/r2 bulk-listers, DuckDB out-of-core aggregation, `diff`/
`series` CLI, chart-lib-free `@disk-tree/react`, npm-dist). Goal: deploy this stack for OA's
other clouds (R2, S3) *and* Ryan's personal cloud accounts, with maximum reuse and per-deploy
customization. Supersedes the earlier config-driven-env framing of this file.

## Decision 1 — graft onto disk-tree (users = forks), via adopt-then-merge (RW, 2026-08-14)

Superseding the earlier "two upstreams, dependency coupling" recommendation after discussion.
The fork model wins because upstream API surfaces that must serve every consumer become
over-abstracted (every FE prop indirected; every upstream tweak = dist SHA + version bump in
some consumer), while branches just set different values/components directly. The killer
evidence was local: marin's tooltip-flicker fix (`b219f47`) changed Treemap *internals* — no
accessor prop could express it; under forks it's one branch commit, CP'd upstream at leisure.
DT's API fitting marin today is survivorship (marin is its only consumer); a second consumer
(projects-axis personal deploy) would either bloat the abstraction or just be a branch.

**Mechanics — option 2 (adopt-then-merge), not rebase:**

1. **Adopt DT on HEAD** (in progress): consume `@disk-tree/react` via `pds local`
   (source-level imports from `~/c/disk-tree/packages/react`) and DT's Python CLI directly —
   the same import shape as post-merge, no pip/npm/SHA in the loop.
2. **When ready: one `git merge --allow-unrelated-histories`** joining DT's history, then
   switch imports to the in-worktree workspace sibling (one-line `package.json` change).
   Rebasing marin's history onto DT was rejected: messy (~90 replayed commits fighting over
   `README.md`/`pyproject.toml`) and it *hides real history*.
3. **Flow rule: CP, not merge.** After the one-time join, deploy branches **cherry-pick**
   upstream (and sibling-deploy) changes — "CP but adapt certain bits to the other side's
   BE/schema" models exactly the intrinsic complexity. Merges drag the whole alternate
   history into a deploy's lineage; avoided.
4. **Alternative FEs are branches too**: DT's Flask+MUI app and marin's CF-Pages app coexist
   as separate branches (not one upstream FE both extend); features move between them by
   CP-with-adaptation.

`main` staying `pip install disk-tree` / npm-dist-able costs nothing and gives non-fork
consumers a shallow on-ramp; forks are the deployment path.

## Decision 2 — repo topology

- **`runsascoded/disk-tree`** (public) — engine + widgets. Gains two new *generic* planes
  (specced in `disk-tree/specs/access-logs-and-cost.md`): access-log ingest and cost import.
- **`Open-Athena/marin-gcs-usage`** (public) — becomes the **app upstream**:
  - `main` = generic multi-store cloud-usage app. Store list, creds env-names, branding, auth
    mode, deploy target all config; attribution/pricing machinery present but **dormant**
    (watchy: "features ship dormant on the base branch").
  - `marin` branch = the OA marin deployment: `identities.yaml`, W&B mining wiring, gcs.oa.dev
    + CF Access config, Batch job schedule, OA branding/OGI. Thin commits: config → enable →
    rebrand → OG → README banner (watchy's exact sequence). Rebases on `main`.
  - **Personal deployment**: can't privately fork a public repo, so: one local clone, two
    remotes (`o` = this repo, `r` = a private personal repo), `rw` branch pushed only to `r`.
    Functionally a private fork; same rebase discipline.
  - Other OA estates (R2, S3) = either config additions on `marin` or sibling instance
    branches (`oa-r2`, …) if they're separate deployments with separate sites.
  - Repo rename (e.g. to something un-marin-specific) is optional and deferred — GitHub
    redirects make it cheap whenever the split lands.

## The three planes × N stores

| Plane | Engine (disk-tree, generic) | App/consumer (per-instance) |
|---|---|---|
| **Size scan** (✅ built) | sharded listers (gcs/s3/r2) + import (listing parquet, SII, S3-Inventory) → canonical layer-2 parquet; diff/series; widgets | schedule (Batch job), store list, attribution overlays, pricing, site |
| **Access logs** (🆕 urgent) | ingest GCS usage-log CSVs / S3 server-access logs / R2 Logpush → canonical access-parquet (`ts,store,bucket,path,op,bytes,requester`) → per-prefix/op/day aggregation reusing the layer-2 path shape; hot-prefix report | log-sink config, requester→identity join (identities.yaml, IP/UA heuristics), Discord/Slack auto-report, ops-treemap view |
| **Cost** (🆕 later) | import billing CSVs (GCP console export now; BQ billing export / AWS CUR / CF GraphQL when available) → canonical cost rows | account/SKU mapping, gross-vs-net policy, reconciliation views |

Store coverage from DT today: gcs/s3/r2 (bulk) + local/ssh (walk). R2 costs plane is weakest
(no per-object access logs without a fronting Worker) — accept degraded granularity there.

## Sequencing (urgent-first)

1. **P0 — access-logs plane v1** (now; `gs://marin-usage-logs` CSVs land any hour). Build the
   ingest + per-prefix/op aggregation **in DT from day 1** (generic CSV schema — it's Google's
   documented usage-log format, nothing marin-specific), thin marin consumer: requester join +
   hot-prefix digest posted to `#internal-discuss`/`#gcs-usage` (Michael Ryan explicitly asked
   for an ops auto-report "like we have for egress and storage"). This both answers the $60K
   reads question and is the first deliverable of the new plane.
2. **P1 — rewire marin onto the DT engine** (unblocked; all DT items landed):
   - Batch job: post-listing step → `disk-tree import --engine duckdb` over the layer-1
     listing parquet (replacing the private aggregation); overlays re-apply on DT's layer-2.
   - Site: swap the DIY treemap for `@disk-tree/react` (`pds gh disk-tree`); DT's Treemap was
     upstreamed *from* this repo's, so this is a reconvergence, not a rewrite.
   - Retire `bucket_list.py` / private compare once proven (keep as reference until then).
3. **P2 — base/instance split** of this repo (watchy playbook): extract the config surface,
   neutralize `main`, cut the `marin` branch carrying only config+branding+domain commits.
4. **P3 — new instances**: personal (`rw` branch → private remote; personal GCS/R2/S3; personal
   CF Pages) and OA's other estates. Each should be ~a config diff at this point.
5. **P4 — cost plane + DT niceties** (BQ export if/when Percy enables it; `storage.md`
   LSM/MoR work; Rust indexer) as demand warrants.

## Governance notes

- Public app upstream fixes the dead `identities.yaml` link for Marin folks and matches the
  reuse goal. For future *more*-sensitive work: add Marin folks to private OA repos instead
  (normalize that), or keep it on the private personal remote.
- Watchy precedent for account separation (personal vs OA infra split, per-owner tokens,
  GitHub-App-vs-PAT direction) applies when instances get their own workers/crons.

## Cross-references

- Engine state + canonical-format rationale: `~/c/disk-tree/specs/gcs-backend-and-snapshot-diff.md`
- New planes spec (DT side): `~/c/disk-tree/specs/access-logs-and-cost.md`
- marin rewiring detail: `./disk-tree-engine-and-multistore.md` (its "Dependencies on the DT
  spec" section is now fully unblocked — items A–E all landed)
- Fork-pattern reference implementation: `~/c/rac/watchy` (`main`(rw)/`oa` split, worker-split.md)
