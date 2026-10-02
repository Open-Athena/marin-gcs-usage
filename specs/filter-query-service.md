# Serving box: a stateful query backend behind the Worker

Status: proposed (2026-10-02). Supersedes this spec's earlier scale-to-zero Cloud Run design (after [phase 0]). Reviewed by the root session. Owner: the gcs session. Code lands on `cloud` (shared); a deployment opts in with its own VM.

## 1. Why

The site's reads run in a Cloudflare Worker: ~128 MB of memory, limited CPU, 6 concurrent subrequests, and no state between requests. Every request re-reads and re-decompresses parquet from GCS. That is why gcs's filters cut out `partial`, and why plain diffs and the series take seconds. The fight is with the serverless setup, not the problem size. On gcs's 2026-10-01 scan ([phase 0]):

- **The data fits on one machine:** 778M rows (objects + dirs), 127M distinct names (about 3 GB as text), and a 7.7 GiB `path` sort.
- **The search is fast when the data is held:** names-first matching takes 0.6–2.4 s end to end in plain DuckDB with the vocabulary in memory, and root sets are identical to a full scan.
- **The full path scan is not an option:** the `path` column is 95 GiB decoded, so a scan takes 34 s warm and 219–288 s cold. Full paths are never scanned; the box keeps the tree as ids.
- **Memory decides the platform:** the vocabulary takes 10–19 GB per generation in DuckDB, and a diff needs two generations, which exceeds Cloud Run's 32 GiB maximum.

## 2. What

An always-on VM, the **serving box**, that acts as a cascading cache in front of the data:

- **Memory:** the hot generations (the latest two, for diffs) in RAM: vocabulary, name → rows map, and per-dir aggregates.
- **Local NVMe:** recently used generations' files, kept under LRU, at block granularity where the cache layer allows (§4.3).
- **GCS:** the parquet files of record, unchanged.

The daily job warms it after each publish. Otherwise, each layer evicts least-recently-used data.

The Worker stays the front door: sign-in (the app's own Google OIDC and email-code sessions in D1, not Cloudflare Access), the edge cache, and every read it already serves well. It forwards to the box only what the box serves better. Phase 1 forwards path filters (`/api/subtree?q=` and `/api/diff?q=`); plain views, diffs and the series move later, only if the bake-off and probes show a gain (§6). The box is a soft dependency: if it's down, the Worker answers as today, with filters flagged `partial` or `approximate`. Never an outage, and never silently short.

## 3. Request flow (filters)

1. The client calls `/api/subtree?q=…` as today.
2. The Worker parses `q` (the shared parser; syntax errors stay 400s). It forwards to the box when the box is configured and the query is heavy: more than ~2K trigram candidates, NOT-only, file-extension-shaped terms, the `regex` syntax on a large store, or a generation without search sidecars. Otherwise it answers itself, and forwards anyway if its answer comes out `partial` or `approximate`.
3. The box returns the same `TreeNode` / `DiffRow` shapes, exact. The Worker caches the answer like any view, keyed by the canonicalised AST (operands sorted, case folded) plus generation, view root, canvas and `minArea`.
4. If the box is down or over its deadline, the Worker returns its own flagged answer.

**Engine override:** `qe=auto|box|worker` on the page URL, passed through to the API (default `auto`). `qe=box` always forwards, for verification; `qe=worker` never does. Every filtered response names the engine that answered and its time.

## 4. The box

### 4.1 Evaluation

- **Roots first:** the search finds only the outermost matching paths; anything under a matching dir is that root's contents. Candidates are checked shallowest-first, and one under an already-found root is skipped before its rows are read.
- **Names-first:**
  - The vocabulary (with a precomputed lowercase column; never `ILIKE`, 6.3 s vs 0.3 s in [phase 0]) is pinned per hot generation.
  - Segment terms are matched against it: substring, `*` within a segment, `^`/`$` anchors, `regex`.
  - Matched name ids map to rows via a pinned name → row-groups map (v1 `rgs`).
  - Roots are an anti-join against matched ancestors, and totals come from the dir aggregates.
- **Drawing:** only what's inside the roots above the view's threshold, level by level (children ordered by size), never whole subtrees.
- **Diff:** both generations in one query.
- **User lens with claims:** the Worker passes the claim regions it already computes.

### 4.2 Engines to bake off

1. **DuckDB names-first** over the generation's files on local NVMe: the [phase 0] approach, served.
2. **Custom in-memory index:** the tree as id arrays (parent, name id, size, counts; ~25–30 GB for 778M rows), the vocabulary as one concatenated byte blob scanned with SIMD `memmem` across cores (~0.1–0.3 s for 3 GB), and children sorted by size.

The bake-off (§6) picks between them, or a hybrid.

### 4.3 Cache layers to bake off

- **gcsfuse's file cache:** whole-file, LRU by size.
- **`rclone mount --vfs-cache-mode full`:** byte ranges, sparse, LRU by size. This is the block-granular option.
- **DuckDB `cache_httpfs`:** a block cache under DuckDB itself.

The deciding questions: time to a hot generation, and how much NVMe a month of generations needs at the access pattern we actually see.

### 4.4 Operations

- **Machine:** a 64 GB VM (n2-highmem-8 or e2-highmem-8; price both, since the work is decode- and string-bound and tolerates e2) with a local SSD, in us-east1 beside the data bucket.
  - On demand for the first month to measure use, then a 1-year commitment.
  - It runs 24/7. A schedule doesn't pay: a commitment bills while stopped, and on demand 16 h a day costs about what a commitment does 24/7.
- **Reachability:** `cloudflared` on the VM, a Cloudflare Tunnel to a hostname gated by a Cloudflare Access service token the Worker presents. No public IP, no load balancer. That token authenticates the Worker to the box only; it has nothing to do with user sign-in.
- **Shape:**
  - A managed instance group of size 1, with autohealing on `/healthz`.
  - A container built from `cloud/` (`dt-cloud serve-query`).
  - A startup script that copies the latest two generations' files to local SSD and loads them. Phase 0 measured copy-then-load at about 8–10 s for the vocabulary.
  - The daily job calls `/reload?gen=…` after publish.
- **IaC:** an `infra/gcp` component, `QueryBox`: its SA with `objectViewer` on the data bucket, the MIG, and the tunnel token secret. gcs's existing stack instantiates it, so no separate stack.
- **Admin status:** an admin panel, read through the Worker from the box's `/status` and the VM's monitoring metrics: up/down, loaded generations, memory, recent query latencies by engine. It has a "reload latest" button.

## 5. Costs

At list prices, approximate:

| Option | Cost per month |
|---|---|
| n2-highmem-8, on demand | ~$380 |
| n2-highmem-8, 1-year commitment | ~$240 |
| e2-highmem-8, on demand | ~$260 |
| Local SSD 375 GB | ~$30 |

There's no egress cost, since the box is in the same region as the bucket. For scale, that's about 1% of the GCS storage bill.

The Worker's search sidecars become an optional fast path: selective queries stay in the Worker, everything else goes to the box. Templating hash-like names (74× fewer trigram postings, [phase 0]) and skewing the index toward drawable or shallow names are ways to keep that fast path cheap enough to build daily.

## 6. Phases

1. **Ground truth and the bake-off harness.** Built 2026-10-02 (`dt_cloud.bench`; results in §6.1).
   - **Query set** (data, per deployment): gcs's is `job/bench-queries.yml`, 35 queries × 3 view roots (`""`, `marin-us-central2`, `marin-us-central2/grug`; `tomat` swaps the last for `marin-us-east5`). Phase 0's four; anchors as they work today (`/tomat`, `tomat/`, `checkpoints/step-`); selective, broad, digit-heavy and hex terms; NOT (including NOT-only), AND, OR; extensions; four `regex` queries. Each entry records why it's in the set; terms were sampled from the 2026-10-01 vocabulary.
   - **Ground truth:** `dt-cloud bench-truth <queries> <gen> -o <truth>` (Batch, n2-highmem-16). Names-first (phase 0's method, v1 `rgs`) wherever the index's lemma bounds the candidates; a one-pass `path` scan for the rest (a term ending in `/`, `regex`), keeping only rows where a half of the predicate becomes true (a regex: every row it holds on). `-c <id>` computes a query both ways and compares. Per query × view: the match roots (count, md5 of the sorted list, the list with each root's net totals when ≤ 50k), the outermost excluded paths, and net totals, with the Worker's semantics (root hit, NOT subtraction; specs/path-store-search.md §1). gcs 2026-10-01: `gs://oa-gcs-usage-dvx/scratch/bench/truth/2026-10-01/`.
   - **Scoring:** `dt-cloud probe -Q <queries> -T <truth> [-c] [-r N] [-x qe=…] [-u <base>]` sends every query × view to `/api/subtree?q=&qs=&full=1`, one at a time, and scores it: `exact` (root set and net totals match, unflagged), `exact*` (exact but flagged), `flagged` (inexact, flagged `partial` / `approximate`), **`FAIL` (inexact and unflagged: a correctness bug)**, `refused` (a 413), `error`. Wall and `server-timing` latency per request; a JSON record per run (`-o …/runs/`). The engine is pluggable (`bench.score.Engine`): the deployed Worker, a local `wrangler` stack with another index variant, the box, or a `qe=` override are all the same `SubtreeEngine` at another base URL or with another param.
2. **Prototype the engines** (§4.2) on a Batch or dev VM against the harness: the Worker index (v1, v2, templated), DuckDB names-first, and the custom in-memory index; cache layers per §4.3. Pick.
3. **`dt-cloud serve-query`**, with parity tests (the shared AST case table, run against the vitest fixtures).
4. **Worker hand-off** behind `QUERY_BOX_URL` (absent = today's behaviour), the `qe=` override, and probe scenarios.
5. **Infra:** the `QueryBox` component plus the tunnel, on demand. Then measure for a month, then commit.
6. **Then, if the probes say so:** plain views, diffs and the series through the box.

### 6.1 Phase 1 results: today's Worker on gcs 2026-10-01 (v1 sidecars)

Truth: 2 Batch jobs (1,213 s + 53 s for an appended query; a 128 s vocabulary-sampling job picked the terms). Names-first and the full scan agree on all three checked queries (`ckpt -eval`, `/tomat`, `model-*-of-00004`), and `tomat` / `grug` / `safetensors` reproduce phase 0's root sets (same md5s). Under full-path semantics `ckpt -eval` has 165 roots and 2 excluded paths at the root (phase 0 tested the last segment only: 170).

Baseline: `dt-cloud probe -Q job/bench-queries.yml -T …/truth/2026-10-01/ -c -r 2`, record `scratch/bench/runs/2026-10-02T19:32:47Z.json`. Of 105 query × view cases: **0 FAIL**, 9 `exact`, 24 `exact*`, 71 `flagged`, 1 `refused` (413, `tensorstore` at the root; on an earlier run it was `ckpt -eval`, so it depends on load). Wall time median 6.9 s, p90 12.1 s, max 16.0 s; `server-timing` median 4.9 s.

Each cell: verdict, roots returned / true roots, flags (P = `partial`, A = `approximate`), median wall time of 2 cold requests.

| query | store root | `marin-us-central2` | `marin-us-central2/grug` |
|---|---|---|---|
| `tomat` | flagged 4/21739 P 9.4s | flagged 0/4 P 7.5s | flagged 1/9 P 15.4s (`marin-us-east5`) |
| `grug` | flagged 13/7724 P 7.4s | flagged 7/5703 P 11.8s | exact* 1/1 A 3.7s |
| `safetensors` | flagged 0/89122 P 5.1s | flagged 0/22899 P 4.5s | flagged 80/240 P 9.1s |
| `ckpt -eval` | flagged 0/165 P 8.3s | flagged 18/22 P 8.2s | exact 0/0 4.3s |
| `/tomat` | flagged 4/400 P 12.1s | exact* 0/0 P 5.9s | exact* 0/0 P 5.4s |
| `tomat/` | flagged 9/22 A 7.8s | exact* 0/0 A 2.2s | exact* 0/0 A 2.7s |
| `checkpoints/step-` | flagged 181/204279 P 6.4s | flagged 581/102269 P 7.6s | flagged 22895/47476 P 9.8s |
| `tensorstore` | refused 10.4s | exact* 1/1 P 12.2s | exact 0/0 4.2s |
| `opt_state` | flagged 0/48615 P 3.9s | flagged 36/39710 P 7.6s | exact* 0/0 P 4.5s |
| `config.json` | flagged 0/527874 P 1.9s | flagged 0/88330 P 4.0s | flagged 3/11 P 10.9s |
| `isoflop-3e+20` | flagged 64/676 P 8.9s | flagged 97/186 P 9.5s | exact 0/0 4.3s |
| `27f2fb` | flagged 25/42 P 12.3s | flagged 1/4 P 8.2s | exact* 0/0 P 7.5s |
| `781bc3291c81ce28` | flagged 0/3 P 4.9s | exact* 0/0 P 5.9s | exact 0/0 1.6s |
| `step-10000` | flagged 3/25666 P 7.6s | flagged 22/9806 P 9.8s | flagged 405/1087 P 13.0s |
| `00001-of` | flagged 0/33355 P 4.8s | flagged 0/8397 P 6.1s | flagged 1/5 P 9.3s |
| `model-*-of-00004` | flagged 0/7138 P 4.4s | flagged 0/2715 P 6.2s | exact* 0/0 P 5.3s |
| `shard_00` | flagged 0/1488639 P 7.3s | flagged 0/226814 P 4.4s | exact* 0/0 P 6.2s |
| `eval` | flagged 12/154194 P 16.0s | flagged 7/61971 P 10.6s | exact* 63/63 P 12.4s |
| `checkpoint` | flagged 147/182512 P 7.4s | flagged 1289/30630 P 8.9s | exact* 1890/1890 P 7.9s |
| `step-` | flagged 80/18695353 P 8.6s | flagged 636/174601 P 7.5s | flagged 22900/47481 P 6.9s |
| `podcast` | exact* 7/7 P 5.3s | exact* 0/0 P 4.4s | exact* 0/0 P 4.5s |
| `grug -checkpoints` | flagged 13/2619 P 9.4s | flagged 6/1451 P 12.5s | exact* 1/1 P 8.0s |
| `-checkpoints` | flagged 1/1 P 3.1s | flagged 1/1 P 3.0s | exact* 1/1 P 9.0s |
| `-tensorstore` | flagged 1/1 P 3.2s | exact 1/1 4.1s | exact 1/1 5.9s |
| `nemotron -dclm\|dolma` | flagged 231/76015 P 9.8s | flagged 103/3036 P 14.6s | exact* 13/13 P 11.6s |
| `qwen sft` | flagged 41/21124 P 13.5s | flagged 12/2834 P 9.3s | exact* 0/0 P 7.5s |
| `tootsie\|isoflop` | flagged 55/8533 P 13.0s | flagged 81/1277 P 11.0s | exact* 541/541 P 8.4s |
| `moe 1e23` | flagged 12/409 P 8.1s | flagged 13/374 P 9.8s | flagged 39/373 P 8.4s |
| `*.pt` | flagged 158/91346 P 8.0s | flagged 0/60292 P 5.7s | exact* 0/0 P 4.9s |
| `.npy` | flagged 0/20392645 P 5.5s | flagged 0/377 P 7.7s | exact* 0/0 P 6.4s |
| `.wav` | exact 128/128 6.9s | exact 0/0 1.4s | exact 0/0 1.7s |
| `\.safetensors$` (regex) | flagged 0/76381 A 2.9s | flagged 0/18223 A 1.6s | flagged 79/235 A 6.2s |
| `/step-[0-9]+$` (regex) | flagged 77/317306 A 6.5s | flagged 607/144822 A 4.6s | flagged 22899/47480 A 5.2s |
| `^marin-us-central2/grug/[^/]*moe[^/]*$` (regex) | flagged 84/1003 A 3.7s | flagged 169/1003 A 4.1s | exact* 1003/1003 A 3.1s |
| `tokenizer\.json$` (regex) | flagged 0/51539 A 2.0s | flagged 0/9832 A 2.2s | flagged 1/5 A 6.2s |

Reading it:

- **The flags hold:** every inexact answer was flagged. Exact answers come from selective terms (`.wav`; `podcast`'s 7 roots, flagged anyway) or small drilled views; at the store root and at the bucket, almost every query is cut by a budget (name groups, path groups or the 1.5 s search time) and returns a small fraction of the roots.
- **A NOT-only query at the store root subtracts nothing.** `-checkpoints` and `-tensorstore` at `""` return exactly the store's total (bytes and objects), while the same queries at `marin-us-central2` subtract. `view.ts` keeps an excluded path only `if (rootFor(e))`, and when the view root is the store root `rootFor` returns `''`, which is falsy, so every exclusion is dropped (the phase-2 loop's `if (!r) continue` likewise drops the drawn rows). Today the `partial` flag from the exclusion search's budget happens to cover it; with a search that completes, the answer would be unflagged and wrong. Fix: compare `rootFor(e) !== null`.
- **`partial` doesn't say which way.** With exclusions cut short, totals come out *over* the truth (`grug -checkpoints`: +4 % bytes at the root, +27 % at `marin-us-central2`; `-checkpoints` at the root: +57 %), while a cut positive search comes out under. A flag that says only "some matches may be missing" understates this.
- **Latency is flat, and slow:** 2–16 s whether the answer is exact or not, because the budgets stop the work. That is the box's target (§1).

## 7. Open questions

- **AST compiler:** where it lives. TS (Worker) and Python or a compiled language (box) both need it. Keep one AST JSON schema with a shared case table.
- **Retention of the Worker's sidecars**, if any are built daily: last ~7 scans plus pinned dates.
- **Anchors in the simple syntax:** `^tomat` / `tomat$` as sugar. `/tomat` and `tomat/` already work as segment-start and segment-end matches.

[phase 0]: filter-query-service-p0.md
