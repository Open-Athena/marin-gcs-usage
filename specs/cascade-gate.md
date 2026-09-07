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
