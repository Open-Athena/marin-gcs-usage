# A.3 gate: DT's cascade vs `webdata`'s path index

The acceptance for `~/c/disk-tree/specs/mgu-scale-unification.md` (DT landed A–E on 2026-09-07; vendored here in `73d57ab`): DT's `import -e duckdb --label usr` on our own listing must reproduce the floor-free path index `webdata` writes — per `(path, usr)`, bytes, objects, class pivots, weighted mtime — at fleet scale, within the daily job's node. Then `viz.py`'s cascade and coarse-tier writer go, and the job calls the engine.

## Pieces

- `gcs-usage labels -l <listing globs> -a <attribution parquets> -o DIR` — mgu's prefix map (rules + attribution parquets + path-glob expansion + identities + the depth-12 cap, the same `viz.prefix_labels` `webdata` uses) as one `labels-<bucket>.parquet` per bucket, `(prefix, usr)` with prefixes relative to the bucket. What `import -L … -c usr` joins by deepest prefix.
- `gcs-usage cascade-a2a -b <bucket> -i <path-index.parquet> <DT dirs tier>` — full outer join on `(path, usr)`; rows only one side has, per-column disagreements with examples, Σ object delta, exit 1 on any. Knows DT's root is `.`, that an empty pivot is NULL here and 0 there, and that mgu's per-object `epoch::BIGINT` rounding moves a weighted mean by up to a second.
- On the node (`mgu` EC2; worktree `~/mgu-gate` on branch `gcs-gate`, venv via `uv pip install -e . -e ./gcs-usage` after `mkdir ui/dist`): `data/` staged with gcsfs, `run-west4.sh` the invocation, `a2a-west4.txt` the report.

## Round 1 — `marin-us-west4`, 2026-09-07 listing (2026-09-07)

`-k 1`, labels, `-p storage_class_id -m -H`, tiers `dirs,coarse` × sort `usr`, `-M 40GB`: 4:03 wall, 21.1 GB peak RSS, 46 partitions. **Bytes, class pivots and mean mtime agree on all 690,708 shared rows and every root slice.** The rest is placeholder semantics: 229 zero-byte `…/` objects (mgu: objects at their dir path; DT: dir markers / trailing-slash files) explain the 43 `o` deltas and 186 mgu-only rows; the two `a//b` names are the 8/14 finding. `-k 2` was killed: every depth-2 *file* became a partition (10,848 of them, ~50 ms each).

Findings and asks written to `~/c/disk-tree/specs/mgu-scale-a3-gate.md`: directory-only partition keys with batching (blocks the fleet run), a placeholder policy (mgu's), the `//` policy, an absolute `--coarse-floor`, and the memory projection.

**Rerun on DT's fix (`46ff7b4`, vendored in `60b734a`)** — `-k 2 --partition-files 4M -F <fleet floor>`: 3,182 directory keys → 4 cascades, 4:06 wall, 22.0 GB peak RSS. `cascade-a2a`: **exact** — bytes, objects, class pivots and mean mtime on all 690,894 shared rows; the only one-sided rows are the two `a//b` names (mgu keeps the empty component) and nine DT empty slices (a dir's own slice with nothing in it), both counted apart.

## Round 2 — the fleet on Batch (submitted 2026-09-07 15:31 UTC)

`job/run.sh` `GATE=1` (with `REPROC=1`): stages the date's listing + attribution, writes labels, runs `import` per bucket under `/usr/bin/time -v` (`GATE_K` partition depth, batching at 4M files, the job's `DUCKDB_MEM`), runs `cascade-a2a` per bucket against the date's published path index, copies logs + reports to `gs://oa-gcs-usage-dvx/gate/<date>/`. Submitted as `gcs-usage-gate-20260907-153058` (`GATE_K=3`, `DUCKDB_MEM` 100GB, n2-highmem-32 / 250 GiB, 1.5 TB local SSD) on the 2026-09-07 listing. To read: peak RSS per bucket against `webdata`'s 141.7 GB, wall against the daily's webdata phase, and six `OK: exact` lines.

### Round 2 results (job `gcs-usage-gate-20260907-153058`, 3h52m total, n2-highmem-32, `-k 3`, batch 4M files, `-M 100GB`)

| bucket | objects | keys → cascades (largest key) | wall | peak RSS | a2a |
|---|---|---|---|---|---|
| marin-us-central2 | 290M | 42,457 → 35 (71.2M files) | 1:28:27 | 100.9 GB | **failed**: DuckDB `Out of Memory Error … 93.1 GiB/93.1 GiB used` |
| marin-eu-west4 | 163M | 24,595 → 17 (84.4M files) | 1:30:06 | 135.2 GB | 719 mgu-only rows (one dropped subtree, see below); else exact |
| marin-us-central1 | 64M | 52,771 → 7 (13.3M) | 15:27 | 58.0 GB | one dropped subtree; `//` rows; else exact |
| marin-us-east5 | 47M | 49,055 → 13 (3.8M) | 14:26 | 48.1 GB | only `//`-collapse rows (`tokenized/gs:/…`); else exact |
| marin-us-east1 | 9M | 19,144 → 3 (1.5M) | 2:21 | 10.5 GB | one dropped subtree; `//` rows; else exact |
| marin-us-west4 | 7.5M | 5,766 → 3 (2.2M) | 1:40 | 8.7 GB | **exact** |

The same day's `webdata` did the whole fleet in **~25 min at 99.1 GB peak** (daily `gcs-usage-snapshot`, PHASE webdata+stage 4103 s minus the 2604 s listing).

Two findings for DT, both in `~/c/disk-tree/specs/mgu-scale-a3-gate.md` round 2:

1. **Dropped subtrees.** In three buckets DT's output lacks a whole subtree and the *bucket root* is short by exactly its bytes, so the rows were lost, not relocated: `datakit/store/_smoke_v0` (677 objects), `sam/results/gpt2-fwe-top50-finetune-cfx-ds-2-url-3/None` (2,001), `julian/datasets/re10k-train-r128-fps30-gop30-crf18-hand21-v2` (143,438 objects, 18 GB). All three are depth-3 directory keys under `--partition-depth 3` in buckets whose partitioning produced a standalone oversized key or many batches — the batching is the suspect.
2. **Oversized keys.** A depth-3 key of 71M (central2) or 84M (eu-west4) files stands alone above the 4M batch and either exhausts DuckDB's cap or spills for 1.5 h. Keys over the batch size need to partition again at depth K+1, recursively.

Everything else agrees: bytes, objects, pivots, mean mtime — the remaining one-sided rows are the `//` policy (DT collapses `…/gs://marin-…/…` names into `gs:/`, mgu keeps the empty component; both ways are internally consistent).

**Verdict:** correctness is one fix away; performance is not there — per-bucket cascades total 3h52m with a failure against `webdata`'s 25 min for the fleet at the same memory. Until partitioning recurses and the per-cascade overhead drops, `viz.py` stays the producer.

### Round 3 results (job `gcs-usage-gate-20260908-110444`, 52m36s total, n2-highmem-32, `-k 3 -P 4000000 -n 16`, `-M 100GB`; DT `bd98e10` = asks 6–9)

Same 2026-09-07 listing as round 2, so a2a and timings are like for like.

| bucket | import wall | peak RSS | cascades | a2a |
|---|---|---|---|---|
| us-east1 | 0:28 | 13.0 GB | 3 | exact (after the `//` class, below) |
| us-east5 | 2:11 | 52.5 GB | 13 | exact (after the `//` class) |
| us-central1 | 2:24 | 59.9 GB | 11 (2 keys split) | exact (after the `//` class) |
| us-central2 | 8:26 → **OOM** | 101.1 GB | 52 (36 keys split, depths 3–7) | no tier written |
| eu-west4 | 20:39 | **168.8 GB** | 74 (2 split) | exact |
| us-west4 | 0:18 | 10.4 GB | 3 | exact |

- **Correctness: the dropped subtrees are gone.** Round 2's three missing subtrees don't recur; every row that both sides emit matches on bytes, objects, class pivots and mtime. The only remaining differences are the `//`-in-name class (ask 9): DT folds `a//b` into `a/b`, so where a bucket holds *both* spellings (`tokenized/gs://marin-us-east5/…` beside `tokenized/gs:/marin-us-east5/…` on east5, 27 rows; `ego-dex/,gs://…` on east1/central1) DT's single-slash rows carry both sets of bytes and mgu's only one. `cascade-a2a` now sets those rows aside (`collapsed_rows`, every row at or under the collapse of an mgu `//` path) instead of failing on them, and the three buckets read exact. One row is still unexplained and tiny: a directory named a single space (`podcast_audio/The_Home_Service_Expert_Podcast/ `, 89 MB, central1) that DT has and mgu doesn't.
- **Speed: 3h52m → 52m for the fleet**, and the per-bucket shape flipped: eu-west4 1:30 → 20:39, central2 from OOM-at-the-first-key to all 52 cascades done in 8 minutes. `webdata`'s 25 min for the same six buckets is now within reach.
- **Two new asks (10, 11 in DT's spec).** central2 finishes every cascade (`dirs_final: 25,374,050 dirs`) and then dies in the final `COPY` — the global sort + parquet write over the union with all 289M file rows — with DuckDB's "failed to allocate 2.0 MiB (93.1 GiB/93.1 GiB used)" at the `-M 100GB` cap, spill dir on the 1.5 TB local SSD. And eu-west4's peak RSS is 169 GB against a 100 GB cap: ~70 GB of process memory DuckDB doesn't account for (the same overshoot `webdata` shows at 141.7 GB), which on the 250 GB node leaves no room to raise `-M`.

Verdict unchanged: `viz.py` stays the producer until central2 completes and the overshoot is understood; the next port is a re-run away.
