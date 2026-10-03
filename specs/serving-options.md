# Serving options for filters, diffs and history: a platform survey

Status: research (2026-10-03). Desk research plus napkin math, no new measurements. It answers "could Lambda / Cloud Run / Modal / a real database replace the [serving box][filter-query-service] or the Worker?". Every price and limit below links to its source. Where a number isn't documented, the text says so instead of guessing. Estimates (marked *est.*) are mine and are what the proposed benchmark would check.

## Baseline (from our own measurements)

- **The Worker** (Pages Functions: 128 MB per isolate, 6 concurrent subrequests): 71 of 105 bench queries come back truncated, with a 6.9 s median.
- **The custom in-memory index on an 8-vCPU VM:** all 105 exact; p50 0.44 s, p90 2.1 s, max 5.2 s. It takes 41 GB of RAM per scan and 138 s to load from GCS.
- **DuckDB names-first over parquet** on the same VM: p50 2.0 s, max 91 s.
- **One scan:** 778M rows and 127M distinct names. The `path` column is 3.6 GiB compressed but 95 GiB decoded. The names file is 1.56 GB, and the vocabulary takes 6.6 GB as Arrow ([phase 0][p0]). Templating digit and hex runs collapses the 127M names to 2.16M templates ([phase 0][p0]).
- **Churn:** gcs's measured per-scan churn is about 0.1–0.2% of rows. A 16-scan SCD-2 consolidation holds 1.027 scan-equivalents of object rows and 1.057 of dir rows ([path-store §4.5][path-store]).

## Summary

Monthly costs assume 1k–5k queries a day ("moderate"). "Idle" means no queries all month. *Est.* marks a figure not yet benchmarked.

| option | broad-filter latency | cold start | $/mo idle | $/mo moderate | 1 scan / 2 scans / history | ops burden |
|---|---|---|---:|---:|---|---|
| Worker (status quo) | 6.9 s median, 71/105 truncated | none | ~0 | ~0 | yes / yes / yes (reads parquet) but truncated | low |
| Serving box, custom index, n2-highmem-8 on demand | 0.44 / 2.1 / 5.2 s (measured) | 138 s, only on restart | 383 | 383 | yes / no (2×41 GB > 64 GB) / no | medium |
| Same box, e2-highmem-8 with a 1-yr commitment | same *est.* (e2 is slower per core) | 138 s on restart | 190 | 190 | yes / no / no | medium |
| Same box on GCE **suspend/resume** | warm: same | resume time undocumented: **measure it** | memory + disk storage only | 0.36–0.52 $/active-h + storage | yes / no / no | medium |
| Same box on **Spot**, with the Worker as fallback | same while up | 138 s after each preemption | up to 91% off on-demand | same | yes / no / no | medium |
| Lambda, one 10 GB function (names-first, S3 range reads) | 2–5 s *est.* | 10–40 s to load the vocabulary; SnapStart restore at multi-GB undocumented | ~0 + GCS→S3 copy (70–150) | 20–60 + copy | vocabulary only; tree read from S3 per query | high (two clouds) |
| Lambda fan-out ("serverless MPP") brute-force `path` scan | 2–4 s *est.* | Python+DuckDB ~1.9 s per cold worker; Rust ~15 ms | ~0 + copy | 1,300–6,600 + copy | yes / yes / yes | high |
| Lambda fan-out, names-first shards | 2–4 s *est.* | as above | ~0 + copy | 90–450 + copy | yes / yes / yes | high |
| Lambda **MicroVMs** (≤ 16 vCPU / 32 GB, suspend/resume) | warm ≈ custom index if slimmed to ≤ ~28 GB *est.* | "within seconds", undocumented at 32 GB | snapshot storage ~3 + copy | 2.02 $/active-h; 120–360 + copy | only if slimmed / no / no | high (launched July 2026) |
| Cloud Run (≤ 8 vCPU / 32 GiB) | doesn't fit 41 GB | = index load (138 s); no snapshot | 0 (or 263 with 1 min instance) | 263+ with a min instance | only if slimmed / no / no | low |
| Cloud Run fan-out over GCS | ≈ Lambda fan-out *est.* | container start undocumented | 0 | ≈ Lambda fan-out, no egress | yes / yes / yes | medium-high |
| GKE Pod Snapshots (gVisor, lazy restore) | warm: same as box | undocumented for 40 GB of CPU memory (GPU examples: 15–37 s) | cluster fee + snapshot storage | node-hours | yes / no / no | high |
| Modal, memory snapshot | warm: same as box | ≥ 20 s for 40 GB at ≤ 2 GiB/s *est.*; documented snapshots go up to 10 GiB | 0 | 0.76 $/active-h × region/non-preemptible multipliers | yes (max memory undocumented) / ? / no | medium |
| Aurora Serverless v2 PG + `pg_trgm` | seconds to minutes *est.* | ~15 s resume (30 s+ after 24 h idle), then a cold cache | storage ~15–40 | 3.84 $/active-h at 32 ACU; ~460 at 4 h/day | yes / yes / yes (SCD-2) | medium |
| BigQuery on demand | 2–5 s *est.* | none | storage, a few $ | ~900–18,000 | yes / yes / yes | low |
| BigQuery editions (Standard, autoscaled) | 2–5 s *est.* | slots scale in seconds | storage only | ~100–500 *est.* | yes / yes / yes | low |
| ClickHouse, self-hosted, disk-backed VM | 0.5–3 s *est.* | none (always on) | 132–264 + disk | same | yes / yes / yes (SCD-2) | medium |
| ClickHouse Cloud (Scale) | as self-hosted once warm *est.* | wake undocumented, then a cold cache | storage | ~0.90 $/active-h for 24 GiB; ~110 at 4 h/day | yes / yes / yes | low |
| MotherDuck (Business) | ≈ DuckDB names-first (p50 2.0 s, max 91 s) | ~100 ms (Jumbo) to a few s (Mega/Giga) | 250 | 250 + 4.80 $/h (Jumbo) | yes / yes / yes | low |

**Short version:** no serverless platform makes the custom index cheaper and as fast. The platforms that can hold it warm (Modal, Lambda MicroVMs, GKE Pod Snapshots, GCE suspend) all pay for a cold restore proportional to its 30–40 GB. None documents that restore at this size, and at about 2 GiB/s it is 15–20 s or more. Fan-out beats the Worker but not the in-memory index, and pays per byte it rescans. The bigger wins are design-side: shrink the index (templates, ids), and store history as SCD-2 intervals. Then a disk-backed database (ClickHouse) or the box on a cheaper footing (a commitment, Spot, or suspend) covers it.

## 1. AWS Lambda instead of Workers

### Limits

- **Memory and CPU:** 128–10,240 MB. CPU scales with memory: one vCPU at 1,769 MB, so about 5.8 vCPU at 10,240 MB ([quotas][lambda-quotas], [memory][lambda-mem]).
- **`/tmp`:** 512–10,240 MB ([quotas][lambda-quotas]).
- **Timeout:** 900 s ([quotas][lambda-quotas]).
- **Payloads:** 6 MB each way for a synchronous call. A streamed response can be 200 MB, uncapped bandwidth for the first 6 MB, then 2 MBps ([quotas][lambda-quotas]). Our `TreeNode` responses (1–2 MB) fit the plain 6 MB limit.
- **Network:** 625 Mbps per execution environment. Functions outside a VPC can request more, scaling with memory up to 3,000 Mbps at 10,240 MB ([quotas][lambda-quotas]). Lambada measured S3 scans saturating at about 90 MiB/s per worker ([Lambada][lambada]).
- **Concurrency:** 1,000 by default. Each function can add 1,000 execution environments every 10 s ([quotas][lambda-quotas]).
- **SnapStart:**
  - Supported on Python 3.12 and later.
  - Not with provisioned concurrency, EFS, or ephemeral storage above 512 MB ([SnapStart][snapstart]).
  - AWS promises startup "as low as sub-second", and warns that infrequently invoked functions "might not experience the same performance improvements" ([SnapStart][snapstart]).
  - No published restore time for a multi-GB snapshot. Community reports are of small functions going from seconds to under one second ([dev.to][snapstart-py]).
  - Price: a cache charge of $0.0000015046/GB-s (minimum 3 h) and $0.0001397998 per GB restored ([pricing][lambda-pricing]). A 10 GB snapshot costs about $39/month to keep cached per published version (one version a day) and $0.0014 per restore.
- **Cold starts:**
  - Python + DuckDB: one benchmark measured 1.85 s cold, including the `httpfs` install, against 0.45 s warm ([dev.to][duckdb-lambda]).
  - Rust (`provided.al2023`): community benchmarks put its init at roughly 15 ms p50, against about 90–170 ms for Python 3.13 ([edjgeek][rust-cold], [nandann][rust-cold2]).

**Does it help?** Against the Worker, yes: 80× the memory, about 6 vCPUs, 15 minutes and ~75 MiB/s from S3. One 10 GB function could pin the names vocabulary (6.6 GB as Arrow, less if templated) and run phase 0's names-first plan. It can't hold the custom index: 41 GB is four times the 10 GB cap.

### Fan-out ("serverless MPP")

**Published systems:**

- **[Lambada][lambada]:**
  - Answers TPC-H queries over S3 at interactive latency (under 10 s, hot or cold) with hundreds to thousands of workers.
  - Invoking 1,000 workers from one driver takes 3.4–4.4 s. Two-level invocation, where workers invoke workers, gets the last one started after about 2.5 s.
- **[Starling][starling]:** cheaper than provisioned systems only when queries arrive 30–60 s or more apart. Its fixed cost is a small coordinator.
- **[Boxer][boxer]:** adds direct function-to-function TCP (NAT punching): up to 11× TPC-H speedups, 621 Mbit/s and under 1 ms RTT between functions.
- **[Skyrise][skyrise]:** comparable to Lambada on scan-heavy queries. A review notes it reports no cold-start numbers.

**Applied to us:**

- **Brute-force `path` scan:**
  - ~110 workers each read about 75 MB of the 8.3 GB `path` file in about 1 s and decode about 0.9 GiB, which phase 0's per-thread rate puts at about 1 s on 6 vCPU.
  - With invocation overhead (0.4–2.5 s), that's 2–4 s *est.*
  - Cost: 110 × 3 s × 10 GB = 3,300 GB-s ≈ $0.044 at the arm rate of $0.0000133334/GB-s ([pricing][lambda-pricing]).
  - That's **$1,300–6,600/month at 1k–5k queries a day**, three to seventeen times the serving box.
- **Names-first fan-out:**
  - About 16 workers each scan a vocabulary shard (~100 MB), then fetch only the matched row groups (0.23–2.36M rows in [phase 0][p0]).
  - About $0.001–0.005 per query, so $90–450/month *est.*
  - Every invocation re-reads its shard unless the environment is warm, so p50 is about 2–4 s *est.*: like DuckDB names-first, not like the in-memory index.

**Verdict:** fan-out turns "truncated" into "exact in a few seconds", which the Worker can't do. It doesn't beat a warm in-memory index, and its cost grows with the bytes each query rescans. It is also a new distributed system: driver, shard plan, stragglers, retries.

### Lambda MicroVMs (new, July 2026)

- **What they are:** Firecracker VMs built from a Dockerfile. Lambda snapshots memory and disk after a `/ready` hook. Each VM gets its own HTTPS endpoint, auto-suspends when idle and auto-resumes on traffic ([concepts][microvm-concepts], [launch blog][microvm-blog]).
- **Limits:** the largest configuration is 16 vCPU / 32 GB ([quotas][lambda-quotas]). A VM runs at most 8 h ([quotas][lambda-quotas]). Arm (Graviton) only. The launch blog names us-east-1 as a region.
- **Pricing:** $0.0000276944 per vCPU-second, $0.0000036667 per GB-second, snapshot storage $0.08/GB-month, $0.00155/GB per snapshot read, $0.0038/GB per write ([pricing][lambda-pricing]).
  - At 16 vCPU / 32 GB that's **$2.02 per running hour**, about four times an n2-highmem-8.
  - Each resume reads about $0.05 of snapshot, and each suspend writes about $0.12.
- **Fit:** this is the closest AWS shape to "the box, scaled to zero". It holds one scan only if the index slims to about 28 GB (the [serving-box spec][filter-query-service] estimates 25–30 GB for the id arrays alone).
- **Unknowns:** resume latency ("within seconds", no figure at 32 GB) and whether memory restores lazily are both undocumented.

### Cost, egress and integration

- **Cost per query and month** (summary table): one 10 GB function at 3k queries a day × 2 s is 1.8M GB-s/month, about $19 after the free tier ([pricing][lambda-pricing]). Fan-out is $90–6,600, depending on the bytes rescanned.
- **The index has to move to S3.** GCS's general data transfer out of Google Cloud is **$0.12/GiB** for the first 10 TiB a month ([GCS pricing][gcs-pricing]). At 20–40 GiB per scan, copied daily, that's **$72–144/month**, plus S3 storage at $0.023/GB-month ([cloudzero][s3-price]): about $14–28/month for 30 days of scans. Copying only an SCD-2 delta (§4.6) would bring the egress to near zero, but then the index has to be rebuilt on AWS.
- **Integration:** the Worker stays the front door and calls a Function URL with a shared secret or SigV4. S3, IAM and CloudWatch are first-class. Everything else (the daily job, D1, the auth gate) stays on GCP or Cloudflare, so we would operate two clouds. AWS internet egress on the responses applies too; its rate isn't checked here.

## 2. GCP's equivalents

- **Cloud Run** (Cloud Run functions deploy onto it):
  - Limits: up to 8 vCPU and 32 GiB per instance, a 4-minute startup timeout and 60-minute requests ([quotas][run-quotas], [memory][run-mem]). A plain HTTP/1 response is capped at 32 MiB; chunked or streamed responses aren't ([quotas][run-quotas]).
  - Request-based billing: active time is $0.000024/vCPU-s and $0.0000025/GiB-s, and **idle minimum instances** are $0.0000025/vCPU-s and $0.0000025/GiB-s ([pricing][run-pricing]). Instance-based billing is $0.000018/vCPU-s and $0.000002/GiB-s ([pricing][run-pricing]).
  - So one warm 8 vCPU / 32 GiB minimum instance costs **about $263/month** idle and $0.98 per active hour.
- **Startup CPU boost:** extra CPU during startup and for 10 s after it, billed while it runs. At 8 vCPU it adds nothing (8 → 8) ([CPU limits][run-cpu]).
- **Snapshot/restore:** Cloud Run has none. The GCP features that do are:
  - **GKE Pod Snapshots**, GA in May 2026 ([docs][gke-snap]): it requires GKE Sandbox (gVisor) and no E2 machines, and restores memory lazily, page-faulting what the app touches first while streaming the rest in the background ([docs][gke-snap], [InfoQ][gke-infoq]). The published examples are GPU models: an 8B model in 15 s and a 70B in 37 s. There's no figure for 40 GB of CPU memory.
  - **GCE suspend/resume**: works for VMs up to 208 GB of memory. The memory is kept in persistent storage, and you pay for storage instead of cores and memory while suspended, for up to 60 days ([suspend][gce-suspend], [overview][gce-suspend-ov]). Resume time isn't documented.
- **Fan-out over GCS:** the same plan works on Cloud Run (concurrency 1, N instances), with free in-region GCS reads ([GCS pricing][gcs-pricing]) and no copy to S3. One vCPU with 2 GiB costs $0.000029/s on Cloud Run, against about $0.0000236/s for 1.77 GB on Lambda arm ([run][run-pricing], [lambda][lambda-pricing]). Per-instance GCS throughput isn't documented.
- **Honest comparison:**
  - Lambda has the finer billing (1 ms, against Cloud Run's 100 ms ([run pricing][run-pricing])), SnapStart and MicroVMs.
  - Cloud Run has 3× the memory, sits next to our data with no egress, and has idle minimum instances priced at about a tenth of the active rate.
  - Neither holds 41 GB. For our data, Cloud Run plus GCE suspend is the more coherent pair. Lambda only wins if MicroVM resume turns out to be fast, which would have to be measured on AWS.

## 3. Modal

- **Pricing:** $0.0000131 per physical core per second (one core = 2 vCPUs) and $0.00000222/GiB-s. Region multipliers are 1.15–1.75×, and non-preemptible runs cost 3× ([pricing][modal-pricing]). An 8-core / 48 GiB container is **$0.76/h** before multipliers.
- **Size limits:** the docs say a maximum is enforced but don't give the number for CPU or memory ([resources][modal-res]). Disk goes up to 3.0 TiB ([resources][modal-res]).
- **Cold start:** "containers boot in about one second" ([cold start][modal-cold]). The idle window (`scaledown_window`) runs from 2 s to 20 minutes ([cold start][modal-cold]).
- **Memory snapshots:**
  - Restore is lazy: gVisor serves pages in the background, prioritizing the ones a process blocks on ([blog][modal-snap-blog]).
  - Snapshots are typically 100 MiB–10 GiB ([blog][modal-snap-blog]). Hosts are expected to download about 2 GiB/s, with "much less effective throughput" in the tail ([blog][modal-snap-blog]).
  - The published examples are 1–3.5 s restores of small snapshots ([blog][modal-snap-blog]).
  - GPU snapshots are alpha ([docs][modal-snap]).
- **Can 20–40 GB restore in seconds?** No: even at the full 2 GiB/s it's 10–20 s, and a broad filter touches most of the index, so lazy restore doesn't save much. Snapshots that size are beyond what Modal documents. **The earlier conclusion still holds:** a cold first query of about 20 s or more, then fast while warm.

## 4. "A proper DB"

Common shape for every engine:

- A `names` table (127M rows, or 2.16M templates plus residuals).
- A `nodes` table (778M rows of ids: `parent`, `name_id`, `size`, counts, `usr`).
- An ancestor or ordinal encoding so "group by child of the view root" is a range or array lookup, not a string prefix.

Querying full `path` strings is out on any engine: 95 GiB decoded per scan.

### 4.1 RDS / Aurora Postgres with `pg_trgm`

- **What the index can do:** GIN/GiST trigram indexes accelerate `LIKE`, `ILIKE`, `~` and `~*`. But "a pattern with no extractable trigrams will degenerate to a full-index scan" ([pg_trgm][pgtrgm]), which is what `step-\d+`-style regexes are.
- **Latency *est.*:** the `names` lookup is fine. The expensive part is the join into 778M `nodes` and the tree rollup. Postgres runs one query on a few cores, so broad filters land in seconds to minutes.
- **Scale to zero:** Aurora Serverless v2 can pause at 0 ACU. Resume is typically about 15 s, and 30 s or more after 24 h paused ([auto-pause][aurora-pause]). It resumes at low capacity and with a cold buffer cache ([auto-pause][aurora-pause]).
- **Pricing:** $0.12 per ACU-hour; 1 ACU ≈ 2 GiB ([Aurora pricing][aurora-pricing]). A cache-resident working set (about 64 GiB) needs about 32 ACU = **$3.84/h**. Storage is $0.10/GB-month ([Aurora pricing][aurora-pricing]).
- **History:** handles it well as an SCD-2 table with range types.
- **Verdict:** poor latency per dollar for this workload.

### 4.2 BigQuery

- **On demand:**
  - $6.25/TiB after the first free TiB a month, with a 10 MB minimum per table and per query ([pricing][bq-pricing]).
  - A names-first query reads the vocabulary (a few GB) plus the matched `nodes` blocks; clustering on `name_id` prunes the rest. That's 5–20 GB *est.*, about **$0.03–0.12 per query** and **$0.9k–18k/month** at 1k–5k queries a day.
- **Editions:**
  - Slots cost $0.04/slot-hour in Standard, $0.06 in Enterprise and $0.10 in Enterprise Plus ([pricing][bq-pricing]).
  - The autoscaler adds slots in steps of 50, keeps them for at least 60 s, and bills a one-minute minimum. Opting in to "fluid scaling" bills per second with no minimum ([autoscaling][bq-autoscale]).
  - At about 200 slot-seconds per query *est.*, 3k queries a day is about $200/month.
- **Search indexes** don't help: they serve `=`, `IN`, `LIKE 'prefix%'`, `STARTS_WITH` and `ENDS_WITH`, "designed for point lookups", with no wildcards ([search][bq-search], [intro][bq-search-intro]). Infix substrings and regexes stay full scans of the names table, which is fine at 2–3 GB.
- **Latency *est.*:** about 1–2 s of job overhead plus execution, so 2–5 s. Not measured; Google doesn't document an interactive latency floor.
- **Storage:** active logical storage costs $0.000031507/GiB-hour, about $0.023/GiB-month ([pricing][bq-pricing]). A decade of SCD-2 history costs a few dollars.
- **Verdict:** zero ops and great for history, but on-demand pricing is ruinous for an interactive UI. Editions make it affordable, at a latency that's probably DuckDB-like.

### 4.3 ClickHouse

**Self-hosted:**

- **Indexes:** the inverted **text index** is GA from ClickHouse 26.2, with `ngrams`, `sparseGrams` and `splitByNonAlpha` tokenizers, and works with `LIKE` and `match` ([text indexes][ch-text]). `ngrambf_v1` and `tokenbf_v1` are now deprecated in its favour ([skip indexes][ch-skip]). Its limits:
  - It can't be materialized on parts over about 4.3B rows.
  - It takes memory to build.
  - The page we fetched listed it as not supported in ClickHouse Cloud; that needs checking against current Cloud releases ([text indexes][ch-text]).
- **Latency *est.*:** a vectorized scan of the names table (a few GB) on 8 cores is well under a second. The `name_id IN (…)` filter and the rollup over `nodes` run multi-core. So 0.5–3 s, with a tail on regexes over the whole vocabulary.
- **Machines:** it's disk-backed, so it doesn't need 41 GB of RAM:
  - e2-highmem-4 (32 GiB) at $0.181/h ≈ $132/month.
  - e2-highmem-8 (64 GiB) at $0.362/h ≈ $264/month, or $0.260/h ≈ $190 with a 1-year commitment ([GCE pricing][gce-gp]).
  - Disk at about $0.10/GiB-month for pd-balanced ([disk pricing][gce-disk]).
- **History:** handles it well, in one SCD-2 `nodes_hist` table.

**ClickHouse Cloud:**

- **Compute** is billed per minute in units of 8 GiB + 2 vCPU and scales to zero when idle ([pricing][ch-pricing]). Scale tier is about $0.2985 per unit-hour on AWS us-east according to third-party teardowns ([getbeton][ch-scale]). ClickHouse's own pricing page showed only $0.32–0.51 for Enterprise ([pricing][ch-pricing]).
- **Idling:** compute isn't billed while paused, storage is. Wake time isn't documented, connections "can time out while it is paused", and it isn't recommended for frequently accessed customer-facing services ([idling][ch-idle]).

### 4.4 MotherDuck

- **Pricing:** Business is $250/org/month plus usage. Pulse is $0.60/CU-hour; Standard $2.40/h, Jumbo $4.80/h, Mega $12/h and Giga $24/h. Storage is $0.04/GB-month ([pricing][md-pricing]).
- **Billing:** wall clock with a 1-minute minimum. The cooldown is configurable from 1 minute to 24 h ([sizes][md-sizes]).
- **Startup:** about 100 ms for Pulse, Standard and Jumbo; a few seconds for Mega and Giga ([sizes][md-sizes]). Mega is 64 cores / 256 GB and Giga 192 cores / 1.5 TB ([blog][md-blog]). Sizes for the smaller tiers weren't found.
- **Latency:** it is DuckDB, so expect our names-first numbers (p50 2.0 s, max 91 s), better on Mega.
- **Verdict:** no gain over self-hosted DuckDB except ops.

### 4.5 Anything better suited

- **Keep the custom index, change its footing.** It's the fastest engine we have, and the box is already a soft dependency: the Worker answers flagged when the box is down ([serving-box spec][filter-query-service]). So:
  - **Spot** is safe: up to 91% off on-demand, with prices changing at most monthly ([GCE pricing][gce-vm]).
  - A **1-year commitment** on e2-highmem-8 costs $190/month ([GCE pricing][gce-gp]).
  - **GCE suspend/resume** keeps the loaded index in suspended memory and charges only storage when idle ([overview][gce-suspend-ov]).
- **Shrink the index.** Templating (127M → 2.16M names, [phase 0][p0]) plus id arrays could bring a scan to ≤ 28–32 GB. That unlocks Cloud Run, Lambda MicroVMs, two scans in a 64 GB box (diffs), or a 32 GB e2-highmem-4.

### 4.6 History: 1e12 rows is the wrong framing

- **The design point:** store history as an interval (SCD-2) table, `(path_id, valid_from, valid_to, size, n_files, usr, …)`, with one row per *version* of a path, not per scan.
  - A scan at time *t* is `valid_from ≤ t < valid_to`.
  - A diff between scans *a* and *b* reads only rows whose interval starts or ends in `(a, b]`, so its work scales with churn, not with table size.
- **Estimate:** rows ≈ N · (1 + c · (S − 1)), for N = 778M rows per scan, S scans and per-scan churn c (new versions as a fraction of rows). Dir rows are included in c because they change whenever a descendant does.
  - **Measured:** gcs c ≈ 0.1–0.2% (median about 0.1% for objects, 0.04% for dirs; two attribution-change days excepted) ([path-store §4.5][path-store]).
  - **1,000 daily scans:** c = 0.1% gives **~1.6B rows**, c = 0.2% gives **~2.3B**. A pessimistic c = 1% still gives only ~8.6B. The naive layout would be 7.8e11.
  - **One year:** about 1.7 scans' worth, ~1.3B rows ([path-store][path-store]).
  - **Caveat:** our listings carry no `updated`, so changes to metadata alone are invisible to this count.
- **What it means:** history is a 2–3× single-scan problem, not a 1,000× one. Every database above holds it on disk. Even the custom index could hold "latest + SCD-2 deltas" instead of two full scans for diffs.

## Recommendation

1. **Don't move to AWS or Modal now.**
   - Lambda helps only through fan-out, which is slower than the custom index and costs more per query, plus $70–150/month of GCS egress and a second cloud.
   - Lambda MicroVMs and Modal can't restore a 30–40 GB index interactively, by their own documented numbers.
   - Revisit MicroVMs only if the index slims to ≤ ~28 GB and AWS publishes resume times.
2. **Keep the custom index and fix its cost:** a 1-year commitment ($190), Spot, or suspend/resume, with the Worker as the fallback that already exists.
3. **Prototype a disk-backed database as the history and diff engine.**

### Proposed benchmark: two candidates against the 105-query bench

**A. Custom index on GCE with suspend/resume, and on Spot**

- Same build as the phase 2 bake-off.
- Measure, at 41 GB loaded:
  - suspend time, resume time and first-query latency after resume (p50 over about 10 cycles, including one after 24 h);
  - the 105-query latencies on e2-highmem-8 against n2-highmem-8;
  - on Spot, preemptions over a week.
- **Cost:** about 10 VM-hours at $0.36–0.52/h, plus a week of Spot e2-highmem-8 and suspended storage. **≈ $15–40.**

**B. ClickHouse 26.2+ self-hosted, on an e2-highmem-8 with pd-balanced**

- Tables: `names` (with a text index, `ngrams`), `nodes` (ids, `ORDER BY (depth, parent, …)`), and `nodes_hist` (SCD-2 over 16 existing scans).
- Run the 105 queries plus a diff set: two adjacent scans, and two scans 15 days apart.
- Pass bar: within 2× of the custom index's p50/p90, with max ≤ 10 s, exact on all 105, and diffs reading only the delta.
- **Cost:** about 20 VM-hours at $0.36/h plus 200 GB of disk for a week. **≈ $10–15.**

**Optional C (≈ $15):** load the same `names`/`nodes` into BigQuery and run the 105 queries on demand. 105 × ~$0.1 is mostly covered by the free TiB. The result is a real latency floor for BQ, to settle §4.2's estimate.

**Decision rule:**

- If A's resume is ≤ ~10 s, run the box suspended (or on Spot) and keep it.
- If B lands within 2×, move diffs and history to ClickHouse and keep the custom index only for filters, or drop it if B is within 1.5×.
- If neither, buy the 1-year commitment and stop.

[filter-query-service]: filter-query-service.md
[p0]: filter-query-service-p0.md
[path-store]: path-store.md
[lambda-quotas]: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
[lambda-mem]: https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html
[lambda-pricing]: https://aws.amazon.com/lambda/pricing/
[snapstart]: https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html
[snapstart-py]: https://dev.to/aws-builders/from-seconds-to-milliseconds-fixing-python-cold-starts-with-snapstart-59mn
[duckdb-lambda]: https://dev.to/aws-builders/serverless-analytics-on-nas-data-for-000001query-duckdb-lambda-x-fsx-for-ontap-2o5o
[rust-cold]: https://edjgeek.com/blog/lambda-cold-starts-dead/
[rust-cold2]: https://www.nandann.com/blog/rust-aws-lambda-production-guide
[lambada]: https://arxiv.org/pdf/1912.00937
[starling]: https://arxiv.org/abs/1911.11727
[boxer]: https://www.cidrdb.org/cidr2021/papers/cidr2021_paper12.pdf
[skyrise]: https://pith.science/paper/2501.08479
[microvm-concepts]: https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html
[microvm-blog]: https://aws.amazon.com/blogs/compute/announcing-lambda-microvms-serverless-compute-environments-with-vm-level-isolation-and-near-instant-startup/
[gcs-pricing]: https://cloud.google.com/storage/pricing
[s3-price]: https://www.cloudzero.com/blog/s3-pricing/
[run-quotas]: https://cloud.google.com/run/quotas
[run-mem]: https://cloud.google.com/run/docs/configuring/services/memory-limits
[run-cpu]: https://docs.cloud.google.com/run/docs/configuring/services/cpu
[run-pricing]: https://cloud.google.com/run/pricing
[gke-snap]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots
[gke-infoq]: https://www.infoq.com/news/2026/09/gke-pod-snapshots-benchmarks/
[gce-suspend]: https://cloud.google.com/compute/docs/instances/suspend-resume-instance
[gce-suspend-ov]: https://cloud.google.com/compute/docs/instances/suspend-stop-reset-instances-overview
[gce-gp]: https://cloud.google.com/products/compute/pricing/general-purpose
[gce-vm]: https://cloud.google.com/compute/vm-instance-pricing
[gce-disk]: https://cloud.google.com/compute/disks-image-pricing
[modal-pricing]: https://modal.com/pricing
[modal-res]: https://modal.com/docs/guide/resources
[modal-cold]: https://modal.com/docs/guide/cold-start
[modal-snap]: https://modal.com/docs/guide/memory-snapshot
[modal-snap-blog]: https://modal.com/blog/mem-snapshots
[pgtrgm]: https://www.postgresql.org/docs/current/pgtrgm.html
[aurora-pause]: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html
[aurora-pricing]: https://aws.amazon.com/rds/aurora/pricing/
[bq-pricing]: https://cloud.google.com/bigquery/pricing
[bq-autoscale]: https://cloud.google.com/bigquery/docs/slots-autoscaling-intro
[bq-search]: https://cloud.google.com/bigquery/docs/search
[bq-search-intro]: https://cloud.google.com/bigquery/docs/search-intro
[ch-text]: https://clickhouse.com/docs/engines/table-engines/mergetree-family/textindexes
[ch-skip]: https://clickhouse.com/docs/optimize/skipping-indexes
[ch-pricing]: https://clickhouse.com/pricing
[ch-scale]: https://www.getbeton.ai/blog/clickhouse-pricing-teardown/
[ch-idle]: https://clickhouse.com/docs/cloud/features/autoscaling/idling
[md-pricing]: https://motherduck.com/product/pricing/
[md-sizes]: https://motherduck.com/docs/about-motherduck/billing/duckling-sizes/
[md-blog]: https://motherduck.com/blog/scaling-duckdb-with-ducklings/
