# R2 serving migration: colocate served artifacts with the serving Worker

Status: **phase 1 + the serve-switch scaffolding built; nothing created or deployed** (2026-09-23, §7). Direction set by the user after the walkDiff-at-scale bench: the fleet-root diff is **network-round-bound against cross-provider GCS**, not CPU-bound — so colocating the served artifacts with the CF Worker is the single biggest speedup, and it saves GCS egress. The base already serves its public Map from R2 (`r2.rbw.sh` = the `disk-tree-demo` Pages project, `wrangler.r2.toml` + `deploy-r2.yml` + `dev.r2.rbw.sh`); **cw is the outlier still serving via `S3Store` over GCS** (`_lib/index.ts`). This is cw *adopting* the base's R2 path, not inventing one.

## 1. Why

Measured in workerd (the real CFW runtime) over a real cw scan pair, fleet root:

- The wall is **I/O-bound**: bytes transfer fast (a single ~16 MB range GET from GCS is sub-second); the cost is the walk's dozens of range GETs across ~10 **serial dependent rounds**, each paying **cross-provider** (CF→GCS) RTT. Real CPU is ~1–1.5 s, well under CFW's 30 s ceiling (the pyrmts `cpuMs` metric over-counts awaited I/O — workerd is single-threaded, yet `cpuMs` ≫ wall, which is only possible if it includes I/O waits).
- Colocated range reads (CF Worker → R2, in-datacenter ~ms) collapse those rounds → wall drops toward the ~1–1.5 s CPU floor (~2× cold). With the immutable-diff edge cache (§4c) repeats are ~instant.
- **Egress:** R2 has zero egress fees; every served read (each diff = dozens of range GETs) stops billing GCS egress. Cost flips to a one-time GCS→R2 copy per artifact, amortized over all reads. Biggest win on the range-heavy diff / subtree / path-index paths.

The principle: **serve derived artifacts from the cloud the serving compute lives in (CF/R2), independent of where the source data or the ingest compute live.** The served artifacts are already copies; their location is a free choice.

## 2. What moves (and what doesn't)

**Moves to R2** (the read path the site serves):
- `snapshots/<scan>/{tree,age,meta,series}.json` + `rules.json` (the `/data/*` proxy).
- The index tiers: `<scan>/index/<gen>/path-index.parquet` + `.groups.json` (+ the coarse tiers + age-pyramids), i.e. what `/api/subtree`, `/api/diff`, `/api/path-index`, and the over-time reader open.
- The `/v1/files` raw-store subset the in-browser parquet viewer browses.

**Stays put:**
- **D1** (marks/owners ledger, `index_schema` pointer) — already CF-native.
- The **source** CoreWeave buckets and the **ingest** that lists them / runs sweeps — Batch is GCP/AWS-only and must sit near the source. Only the *publish* of served artifacts changes.
- The internal access-log store (not on the hot serve path).

## 3. The pipeline shape: build on Batch, publish to R2

Ingest stays on GCP Batch (heavy compute + CoreWeave access). A **final publish step** copies the served artifacts to R2 — the general "build where the source+compute is, publish to where serving is" pattern the user wants as a standard last stage. Options, cheapest first:
- **Publish-only-what-serves:** after the existing index build, `cp` the served subset (§2) GCS→R2 (one-time GCS egress per artifact; R2 ingress free). Keep GCS as source-of-truth/fallback initially.
- Later: write the served artifacts to R2 directly from the job (skip GCS for them), keeping GCS only for the raw listings the ingest reconsumes.

Idempotent, additive: new scans publish to R2; existing scans backfill by `cp`.

## 4. Serving switch + the fast-path (storage-independent) work

- **Store binding — resolved by §7: the base's S3-over-R2 `STORE_*` seam, not a native binding.** `r2.rbw.sh` serves via `S3Store` against R2's S3 endpoint (`STORE_ENDPOINT`/`STORE_ACCESS_KEY_ID`/`STORE_SECRET_ACCESS_KEY` secrets + `STORE_BUCKET`/`STORE_REGION="auto"` vars), falling back to GCS + `GCS_HMAC_*` when unset. cw now ports that seam (default GCS, byte-equivalent keys); the flip is setting the `STORE_*` vars/secrets. A native binding would diverge from the base for a marginal win — a base-level proposal for later, if ever. The pyrmts `Storage {head,getRange,get}` wraps the same store for walkDiff/subtree.
- **(a) digest quotas — DONE** (2026-09-23): `cw_digest.BUCKET_QUOTA` now the CW-authoritative 910 TiB / 100 TiB (`cwobject_quota_info` via finelog), explicit `1P`/`100Ti` labels, hero's zone-shared caveat. Independent of R2.
- **(b) `b_max` group-prune — measured, DEFERRED.** Diagnostic on the real pair (`tmp/walkdiff-bench/diag-bmax.mjs`): of 338 expansions, 38 (11%) had every child sub-floor (the skippable set) and 104 (31%) were leaf-listings (an object expanded as a dir — no `kind` column). But `list()` already short-circuits an empty locate range and those reads mostly hit `rgCache` (1004 hits vs 76 RG reads), so the waste is decode/post-filter **CPU, not GETs or rounds** — it cannot touch the round-bound wall. Ceiling ≈ 10–40% CPU/bytes; needs a pyrmts change (`RowGroupSummary.sizeMax` + the walk consuming it) and a semantic call (it drops sub-floor one-sided rows: render-equivalent, not output-identical). Filed as a pyrmts ask (`sizeMax` + a leaf/has-children signal); revisit after R2 + KV/warm.
- **(c) immutable-diff edge cache — already existed (2026-09-15 diff-perf); two cw gaps closed.** `api/diff.ts` caches the full response in the two-tier colo-Cache-API + global-KV `edgeCache.ts` under a complete key (pair, path, viewport, lens, owner, marks, class, query, depth; versioned by `CACHE_V`), so repeat views in a colo are already instant. Gaps: (i) cw never bound the global `CACHE_KV` tier → the IaC now provisions `oa-cw-s3-usage-cache` (checklist 1–2); (ii) cw's job never warmed → `job/cw-run.sh` stage **4b** runs `dt-cloud warm-cache` (dry-run validated: the home subtree + the adjacent 12 h pair + the 1d/3d/7d/14d/30d chips × 5 canvas widths), gated on the CF Access service-token pair or a site token (cw-s3.oa.dev is whole-host Access-gated) — a cw Batch secret to provision. KV and warm go together: without KV, a warm from us-central1 heats one colo.

Combined: repeats are instant today; the cold fleet-root goes sub-second only once R2 collapses the rounds (§1). That is the "blazingly fast" path — R2 first, then KV + warm for the first view, (b) as a CPU trim after.

## 5. Base coordination

The base already serves from R2, so most of this is cw catching up, which **shrinks cw's GCS-specific deployment delta** (a win for the disk-tree-as-base rebase). Any change the base itself needs (e.g. the R2 binding shape, or `buildDiff`/subtree reading through the binding rather than S3) is flagged to the disk-tree session via the mgu handoff (`~/c/oa/marin-gcs-usage/specs/disk-tree-as-base.md`). The gcs deployment also serves from GCS today — whether it migrates too is a parallel decision (same mechanism).

## 6. Phases

1. **Publish step** (§3 option 1): `cp` the served subset GCS→R2 for new + backfilled scans; keep GCS. No serve change yet.
2. **Serve switch behind a flag:** cw reads the served subset from R2; verify byte-equivalent answers vs GCS on `/data/*`, `/api/subtree`, `/api/diff`, `/api/path-index`, over-time. Re-run the walkDiff bench CF→R2 to confirm the round collapse.
3. **Cut over** cw serving to R2; GCS stays fallback/source-of-truth for one cycle, then the served subset can stop writing to GCS.
4. **Fast-path:** (a) shipped; (c)'s KV + warm land with checklist items 1–2 and the `:cw` rebuild; (b) deferred (§4).

## 7. Built 2026-09-23 (phase 1 + the serve-switch scaffolding; nothing created or deployed)

**What the base does (matched, not reinvented):** `r2.rbw.sh` is **S3-over-R2, not a native R2 binding** — `wrangler.r2.toml` sets `STORE_BUCKET`/`STORE_REGION="auto"` as vars and `STORE_ENDPOINT`/`STORE_ACCESS_KEY_ID`/`STORE_SECRET_ACCESS_KEY` as Pages secrets; `_lib/index.ts` `storeCreds`/`storeReady`/`makeStore` fall back to the GCS defaults + `GCS_HMAC_*` when `STORE_*` is unset. The base's `data/[[path]].ts` uses the seam; its `v1/files` still hardcodes GCS. §4's "prefer the native binding" is therefore **dropped** — matching the base is worth more than the binding's marginal win (and it keeps cw's deployment delta small for the rebase); a native binding is a base-level upgrade to propose to disk-tree later, if ever.

**cw site (`wt/cw-s3/site`) — the flag is the base's `STORE_*` seam, default GCS, byte-equivalent reads (same keys):**
- `functions/_lib/index.ts`: ported `storeCreds` + `storeReady`, added `storeTarget` (endpoint/bucket/region in one place), env-generalized `makeStore` (prefix default keeps `cw-l2/`).
- `functions/_lib/auth.ts`: `GCS_HMAC_*` now optional; `STORE_ENDPOINT|BUCKET|REGION|PREFIXES|ACCESS_KEY_ID|SECRET_ACCESS_KEY` on `Env`.
- On the seam: `data/[[path]].ts`, `v1/files/[[path]].ts` (allow-list gains `cw-l2/`), `api/todo.ts`, `api/path-index.ts` (allow-list gains `cw-l2/`), and the readiness guards in `api/subtree|age|age-pyramid|series|estate|sweep-owners|marks/totals.ts` (`storeReady(env)`). Dead per-file `BUCKET` consts removed. `api/diff*` + `edgeCache.ts` untouched (owned by the edge-cache work).
- `wrangler.toml`: the R2 flip documented as a commented `STORE_*` stanza; the `CACHE_KV` stanza filled with the IaC output name, still commented (a bogus id breaks `wrangler pages dev`).
- `_lib/index.test.ts`: `store seam` specs. `tsc -p functions` clean; `pnpm test` 129/129; `pnpm build` OK.

**Publish step (`cloud/`):** `dt-cloud publish-r2 [-b BUCKET] [-n] [-p PREFIX…] [-s SUBDIR] [-w N] SCAN` (`src/dt_cloud/publish.py`). Served subset = `snapshots/<subdir>/<scan>/` + `cw-l2/<scan>/` (layer-2 parquets + `index/<gen>/` tiers, `.groups.json`, age pyramids, over-time). Streams GCS→R2 (S3 API, `boto3` via the existing `s3` extra), idempotent on size + md5 (the GCS md5 is stamped as `gcs-md5` object metadata because multipart ETags aren't md5s), `-n` lists what it would copy. Env: `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. `tests/test_publish.py` (exact-equality specs for the prefixes, md5 conversion, HEAD parsing, the copy decision, the summary line). **Job slot (not wired):** after `index-sync`/`index-gc` and **before the 4b warm-cache stage** — once the store is flipped, the warm must hit a scan already in R2 — and before step 5 (digest): `dt-cloud publish-r2 "$SNAP_ID" || echo "WARN: publish-r2 failed (site keeps serving GCS)" >&2`, gated on `R2_ENDPOINT:+set` like the other optional stages; the `R2_*` env arrives from Secret Manager via `cw-batch-submit.sh` like `AWS_*`.

**IaC (`~/c/oa/marin-gcs-usage/cf/`, NOT `ops/cf/gcs-usage` — the `cf-iac` memo's path is stale):** `Store` gains `r2_bucket`/`r2_location`/`r2_token_permission_groups[_ids]`; `CfnDashboard` adds children `r2` (`R2Bucket`) and `r2-token` (`ApiToken`, R2 bucket-item read+write, account-scoped; bucket-scoping is a follow-up once the bucket id exists), exporting `r2_bucket`, and as secrets `r2_s3_access_key_id` (= token id) and `r2_s3_secret_access_key` (= sha256 of the token value — R2's S3-credential derivation, so the token *is* the credential). cw-s3's `Store` now sets `kv_name="oa-cw-s3-usage-cache"` (the `CACHE_KV` tier) and `r2_bucket="oa-cw-s3-usage-index"`; `__main__.py` exports `cache_kv_id` + the R2 outputs. Both stacks construct under Pulumi mocks (cw-s3 registers the 9 outputs; gcs unchanged). Permission groups resolve by name via `get_api_token_permission_groups_list_output` — a read-scoped CI token can't list them, so CI `preview` needs `r2_token_permission_group_ids` set explicitly. **Backfill: Super Slurper (one-time bulk, console) + the publish step for steady state; Sippy not recommended** (needs a GCS SA key in CF, makes first reads cross-provider, redundant once every scan publishes) — reasoning in `cf/README.md`.

**User-gated checklist (in order):**
1. `cf/`: `pulumi up` on the `cw-s3` stack → creates the KV namespace, the R2 bucket, and the R2 API token (needs a token with KV + R2 + API-token-write scopes — the wrangler-dev token has none).
2. `site/wrangler.toml`: paste `cache_kv_id` into the `[[kv_namespaces]]` stanza and uncomment it (the edge cache's global tier, independent of R2).
3. Secrets: `wrangler pages secret put STORE_ENDPOINT|STORE_ACCESS_KEY_ID|STORE_SECRET_ACCESS_KEY --project-name oa-cw-s3-usage` from the stack outputs; the same three as `R2_*` (+ `R2_BUCKET`) into Secret Manager for the Batch job; plus the CF Access service-token pair (`CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET`) as a Batch secret for the 4b warm-cache stage.
4. Backfill: Super Slurper over `snapshots/cw/` + `cw-l2/` GCS→R2 (or `for s in $(scans); do dt-cloud publish-r2 $s; done` on node `mgu`).
5. Wire `dt-cloud publish-r2 "$SNAP_ID"` into `job/cw-run.sh` (slot above) and rebuild `:cw`.
6. Flip: set `STORE_BUCKET`/`STORE_REGION="auto"` in `wrangler.toml` `[vars]`, deploy, verify byte-equivalence (`/data/cw/scans.json`, `/api/subtree`, `/api/diff`, `/api/path-index`, over-time) and re-run the walkDiff bench against R2.

**To flag to disk-tree (base):** (i) `v1/files/[[path]].ts` on the seam + `storeTarget` — cw now generalizes the raw-store proxy the base still hardcodes to GCS; (ii) `STORE_PREFIXES` default gaining `cw-l2/` in the browser allow-list is cw-only.
