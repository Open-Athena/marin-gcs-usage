"""Sweep executor — dry-run by default (specs/sweep-executor.md phase 3).

Consumes a `sweep manifest` plan dir. Per eligible directory: fresh re-list
(captures generations — the pinned listing has none), intersect with the
manifest, verify `timeCreated` matches (an overwrite since the scan keeps the
object), detect drift (new keys under a swept dir → skip the dir by default),
and — only with ``--for-real`` — issue generation-matched batch deletes.

Every decision lands in a per-bucket log parquet under the plan dir
(``would-delete/`` or ``deleted/``): name, size, generation, decision.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial

err = partial(print, file=sys.stderr)

#: Per-key decisions (the log's `decision` column).
DECISIONS = (
    "delete",              # in manifest ∩ live, created matches → deleted (or would be)
    "skipped_gone",        # in manifest, no longer live — graceful no-op
    "skipped_overwritten", # live but created moved — rewritten since the scan; keep
)

BATCH = 100  # GCS JSON batch limit per request


def list_roots(dirs: set[str], approved: tuple[str, ...], bucket: str) -> list[str]:
    """Prefix-free listing roots covering every manifest dir: each dir cut to
    one segment below its band (its top-level segment when no band covers
    it), so a band fans out into its children's listings; a dir that *is* its
    band (or a root's ancestor) becomes the root itself and swallows the
    deeper ones. `''` = the whole bucket."""
    root_of_band: dict[str, int] = {}
    for a in approved:
        pre = f"gs://{bucket}/"
        if a.startswith(pre):
            rel = a[len(pre):].rstrip("/")
            root_of_band[rel] = (rel.count("/") + 1) if rel else 0
    roots: set[str] = set()
    for dn in dirs:
        hit = max((r for r in root_of_band if dn == r or (dn.startswith(r + "/") if r else True)), key=len, default=None)
        depth = root_of_band[hit] if hit is not None else 0
        roots.add("/".join(dn.split("/")[: depth + 1]) if dn else "")
    out = sorted(roots)
    pruned: list[str] = []
    for r in out:
        if any(r == p or (r.startswith(p + "/") if p else True) for p in pruned):
            continue
        pruned.append(r)
    return pruned


def execute_plan(
    plan_dir: str,
    for_real: bool = False,
    only_buckets: tuple[str, ...] = (),
    drift: str = "skip",  # skip | proceed — dirs that gained NEW keys since the scan
    workers: int = 8,
    min_soft_delete_days: int = 7,
    client=None,
    reclassify=None,  # (bucket, dir, approved) -> category at the CURRENT ledger head; non-eligible dirs are skipped (ledger drift). `approved` is the plan's approved bands — without them, band-approved dirs would all reclassify as deferred and be dropped.
) -> dict:
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq
    from google.cloud import storage

    fs, ppath = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{ppath}/plan-summary.json") as fh:
        plan = json.load(fh)
    client = client or storage.Client()
    approved = tuple(plan.get("approved") or ())

    def band_of(bucket: str, dn: str) -> str:
        p = f"gs://{bucket}/{dn}/" if dn else f"gs://{bucket}/"
        hits = [a for a in approved if p.startswith(a)]
        if hits:
            return max(hits, key=len)
        top = dn.split("/", 1)[0] if dn else ""
        return f"gs://{bucket}/{top}/" if top else f"gs://{bucket}/"

    mode = "deleted" if for_real else "would-delete"
    summary: dict = {"plan": plan_dir, "for_real": for_real, "drift": drift, "buckets": {}}
    log_schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()), ("generation", pa.int64()),
        ("decision", pa.string()), ("dir", pa.string()),
    ])

    for bucket, binfo in plan["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        if "eligible" not in binfo:
            continue
        mpath = f"{ppath}/manifest/{bucket}.parquet"
        if not fs.exists(mpath):
            raise SystemExit(f"plan says {bucket} has eligible keys but {mpath} is missing")
        with fs.open(mpath, "rb") as fh:  # deterministic close: see `sweep manifest`
            mf = pq.read_table(fh).to_pandas()
        if for_real:
            _require_soft_delete(client, bucket, min_soft_delete_days)
        by_dir = {dn: g for dn, g in mf.groupby("dir")}
        ledger_drift: list[str] = []
        if reclassify is not None:
            still = {}
            for dn, g in by_dir.items():
                if reclassify(bucket, dn, approved) == "eligible":
                    still[dn] = g
                else:
                    ledger_drift.append(dn)
            by_dir = still
        err(f"{bucket}: {len(mf):,} manifest keys in {len(by_dir):,} dirs ({mode})"
            + (f" — {len(ledger_drift):,} dirs dropped by newer marks" if ledger_drift else ""))
        bkt = client.bucket(bucket)
        counts: Counter = Counter()
        drift_dirs: list[dict] = []
        rows: list[dict] = []

        # One recursive listing per *root* (a band's child directory, or the
        # band itself when it is directly eligible) instead of one per
        # directory: 1.4M eligible dirs would be 1.4M list calls; the roots are
        # a few thousand, each a streamed page walk. GCS lists names in
        # lexicographic order and the manifest is sorted the same way, so each
        # root is a merge: manifest-only → gone, both → created check, live-only
        # under a manifest dir → drift for that dir. A directory's decisions are
        # buffered until the listing has moved past it (its keys are contiguous
        # under `dn/`, nested dirs form a stack), and only then deleted — drift
        # discovered late still gates the whole directory.
        dirs_all = set(by_dir)
        mf_sorted = mf[mf["dir"].isin(dirs_all)].sort_values("name", kind="stable")
        roots = list_roots(dirs_all, approved, bucket)

        def do_root(root: str):
            prefix = f"{root}/" if root else ""
            sub = mf_sorted[(mf_sorted["dir"] == root) | mf_sorted["dir"].str.startswith(prefix)] if root else mf_sorted
            want = iter(sub[["name", "size_bytes", "created", "dir"]].itertuples(index=False, name=None))
            w = next(want, None)
            pend: dict[str, dict] = {}
            stack: list[str] = []
            done: list[tuple] = []

            def flush(dn: str) -> None:
                p = pend.pop(dn)
                todo, out = p["todo"], p["out"]
                drifted = p["extra_o"] > 0
                if drifted and drift == "skip":
                    done.append((dn, out, {"dir": dn, "new_objects": p["extra_o"], "new_bytes": p["extra_b"], "skipped_deletes": len(todo)}, 0))
                    return
                if for_real:
                    for i in range(0, len(todo), BATCH):
                        # raise on failure: a 412 (generation moved) or transient
                        # error aborts loudly; a re-run resumes via skipped_gone
                        with client.batch():
                            for blob in todo[i : i + BATCH]:
                                bkt.delete_blob(blob.name, if_generation_match=blob.generation)
                deleted_b = 0
                for blob in todo:
                    out.append({"name": blob.name, "size_bytes": int(blob.size or 0), "generation": int(blob.generation), "decision": "delete", "dir": dn})
                    deleted_b += blob.size or 0
                done.append((dn, out, ({"dir": dn, "new_objects": p["extra_o"], "new_bytes": p["extra_b"], "skipped_deletes": 0} if drifted else None), deleted_b))

            def settle(name: str) -> None:
                # close every open dir the listing has moved past
                while stack and stack[-1] != "" and not name.startswith(stack[-1] + "/"):
                    flush(stack.pop())

            def ensure(dn: str) -> dict:
                if dn not in pend:
                    pend[dn] = {"todo": [], "out": [], "extra_o": 0, "extra_b": 0}
                    stack.append(dn)
                return pend[dn]

            def gone(row) -> None:
                name, size, _created, dn = row
                settle(name)
                ensure(dn)["out"].append({"name": name, "size_bytes": int(size), "generation": 0, "decision": "skipped_gone", "dir": dn})

            for blob in client.list_blobs(bucket, prefix=prefix):
                n = blob.name
                while w is not None and w[0] < n:
                    gone(w)
                    w = next(want, None)
                settle(n)
                dn = n.rpartition("/")[0]
                if w is not None and w[0] == n:
                    p = ensure(w[3])
                    created = blob.time_created.replace(tzinfo=dt.timezone.utc) if blob.time_created.tzinfo is None else blob.time_created
                    if abs((created - w[2].to_pydatetime()).total_seconds()) > 1:
                        p["out"].append({"name": n, "size_bytes": int(w[1]), "generation": int(blob.generation), "decision": "skipped_overwritten", "dir": w[3]})
                    else:
                        p["todo"].append(blob)
                    w = next(want, None)
                elif dn in dirs_all:
                    p = ensure(dn)
                    p["extra_o"] += 1
                    p["extra_b"] += blob.size or 0
            while w is not None:
                gone(w)
                w = next(want, None)
            while stack:
                flush(stack.pop())
            return done

        total_deleted_b = 0
        bands: dict[str, Counter] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for done in pool.map(do_root, roots):
                for dn, out, drifted, dbytes in done:
                    rows.extend(out)
                    band = bands.setdefault(band_of(bucket, dn), Counter())
                    if drifted:
                        drift_dirs.append(drifted)
                        band["drift_new_objects"] += drifted["new_objects"]
                    total_deleted_b += dbytes
                    for r in out:
                        counts[r["decision"]] += 1
                        if r["decision"] == "delete":
                            band["bytes"] += r["size_bytes"]
                            band["objects"] += 1
                        elif r["decision"] == "skipped_gone":
                            band["gone"] += 1
                        else:
                            band["overwritten"] += 1

        log_path = f"{plan_dir}/{mode}/{bucket}.parquet"
        lfs, lpath = fsspec.core.url_to_fs(log_path)
        lfs.makedirs(lpath.rsplit("/", 1)[0], exist_ok=True)
        # Small row groups: the site's parquet viewer pages *within* a row
        # group, so a 1M-row group (~27 MB) is fetched to show 100 rows. 64k
        # rows (~1.7 MB) keeps a page cheap; the file is written once, read
        # many times.
        pq.write_table(pa.Table.from_pylist(rows, schema=log_schema), lpath, filesystem=lfs, row_group_size=65_536)
        summary["buckets"][bucket] = {
            "decisions": dict(counts),
            "delete_bytes": total_deleted_b,
            "drift_dirs": drift_dirs,
            "ledger_drift_dirs": ledger_drift,
            "bands": {b: dict(c) for b, c in bands.items()},
        }
        err(
            f"  {bucket}: {counts['delete']:,} {mode} ({total_deleted_b / 1e12:.2f} TB), "
            f"{counts['skipped_gone']:,} gone, {counts['skipped_overwritten']:,} overwritten, "
            f"{len(drift_dirs):,} drifted dir(s){' (skipped)' if drift == 'skip' else ''}"
        )

    with fsspec.open(f"{plan_dir}/{mode}-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    summary["_plan"] = plan
    return summary


def run_id_for(plan: dict, started_ts: int) -> str:
    return f"{plan['date']}-h{plan['head']}/{dt.datetime.fromtimestamp(started_ts, dt.timezone.utc):%Y%m%dT%H%M%SZ}"


def record_run_start(plan: dict, plan_dir: str, exec_head: int, actor: str, started_ts: int, for_real: bool) -> str:
    """Insert the run's D1 row as soon as it starts (`finished_ts` NULL, zero
    totals) so the console lists it while it runs; `record_run` fills it in."""
    from .index_footer import _creds, _d1_query, _q

    run_id = run_id_for(plan, started_ts)
    tok, acct = _creds()
    _d1_query(
        "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, "
        "deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, "
        "undo_deadline, log_dir) VALUES ("
        f"{_q(run_id)}, {_q(plan_dir)}, {_q(plan['date'])}, {plan['head']}, {exec_head}, {_q(actor)}, "
        f"{_q('real' if for_real else 'dry')}, {started_ts}, NULL, 0, 0, 0, 0, 0, 0, NULL, {_q(plan_dir)})",
        acct, tok,
    )
    return run_id


def record_run(
    summary: dict,
    plan: dict,
    exec_head: int,
    actor: str,
    started_ts: int,
    finished_ts: int,
    soft_delete_days: int = 7,
) -> str:
    """Persist the run + per-band rows to D1 (migration 0015) — deletions as
    first-class records the site can surface per path. Returns the run_id.
    Completes the row `record_run_start` opened (or inserts it, for a run that
    skipped the start record)."""
    from .index_footer import _creds, _d1_query, _q

    mode = "real" if summary["for_real"] else "dry"
    run_id = run_id_for(plan, started_ts)
    tot = Counter()
    band_rows = []
    for bucket, b in summary["buckets"].items():
        d = b.get("decisions", {})
        tot["deleted_objects"] += d.get("delete", 0)
        tot["deleted_bytes"] += b.get("delete_bytes", 0)
        tot["skipped_gone"] += d.get("skipped_gone", 0)
        tot["skipped_overwritten"] += d.get("skipped_overwritten", 0)
        tot["drift_dirs"] += len(b.get("drift_dirs", []))
        tot["ledger_drift_dirs"] += len(b.get("ledger_drift_dirs", []))
        for prefix, c in (b.get("bands") or {}).items():
            band_rows.append(
                f"({_q(run_id)}, {_q(prefix)}, {c.get('bytes', 0)}, {c.get('objects', 0)}, "
                f"{c.get('gone', 0)}, {c.get('overwritten', 0)}, {c.get('drift_new_objects', 0)}, 0)"
            )
    undo = f"{finished_ts + soft_delete_days * 86400}" if mode == "real" else "NULL"
    tok, acct = _creds()
    _d1_query(
        "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, "
        "deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, "
        "undo_deadline, log_dir) VALUES ("
        f"{_q(run_id)}, {_q(summary['plan'])}, {_q(plan['date'])}, {plan['head']}, {exec_head}, {_q(actor)}, "
        f"{_q(mode)}, {started_ts}, {finished_ts}, {tot['deleted_bytes']}, {tot['deleted_objects']}, "
        f"{tot['skipped_gone']}, {tot['skipped_overwritten']}, {tot['drift_dirs']}, {tot['ledger_drift_dirs']}, "
        f"{undo}, {_q(summary['plan'])}) "
        "ON CONFLICT (run_id) DO UPDATE SET finished_ts = excluded.finished_ts, deleted_bytes = excluded.deleted_bytes, "
        "deleted_objects = excluded.deleted_objects, skipped_gone = excluded.skipped_gone, "
        "skipped_overwritten = excluded.skipped_overwritten, drift_dirs = excluded.drift_dirs, "
        "ledger_drift_dirs = excluded.ledger_drift_dirs, undo_deadline = excluded.undo_deadline",
        acct, tok,
    )
    if band_rows:
        _d1_query(
            "INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES "
            + ", ".join(band_rows),
            acct, tok,
        )
    return run_id


def _require_soft_delete(client, bucket: str, min_days: int) -> None:
    b = client.get_bucket(bucket)
    pol = b.soft_delete_policy
    secs = (pol.retention_duration_millis or 0) / 1000 if pol else 0
    if secs < min_days * 86400:
        raise SystemExit(
            f"{bucket}: soft delete retention {secs / 86400:.0f}d < required {min_days}d — refusing --for-real"
        )
