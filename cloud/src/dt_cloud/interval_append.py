"""The interval store's per-scan append (specs/interval-store.md §2.7): each new scan becomes a small **run**
beside an immutable base generation, published by an immutable per-scan manifest, merged into bigger runs on a
binary counter, and read by combining base and runs (rows of one version: the smallest `vt` wins). The machinery is
`static_append`'s (the static name index's runs); the rows are the store's.

Per scan `D`, under `interval-store/<gen>/deltas/<D>/` (data bucket):

1. `prepare` (local): pin the next scan's `path` sort (`scans.json`), refusing any but the next.
2. `ranges` (key ranges, Batch): per range, the open versions (the previous scan's state, or the base's range files)
   and `D`'s rows → `pyrmts.intervals.append_intervals` for the three version tables the store serves:
   - `pvl`: one row per path, `last_read` in the key (`interval_store.fold`'s versions);
   - `sv`: owner slices (`build -S`);
   - `svt`: owner slices split where their path's version changes, with its total (`fold -S`).
   Each writes its delta (`pvl|sv|svt/r####.parquet`: opened versions, `op` 1, and close records — the closed
   version's row with its final `vt` — `op` −1) and the next open state (`state/<D>/…`, scratch bucket); the range's
   `ranges/r####.json` (its counts and reconstruction check) is written last.
3. `publish` (one task): cut the run's served sorts (`served/<sort>.parquet` + `.groups.parquet`, the five the
   Worker reads), carry the binary counter (two runs of one level merge into one a level up: per range
   `pyrmts.runs.merge_parquets` on the deltas, min-`vt`, then the merged run's own cut), then
   `manifests/<D>.json`, written once and only after every file of every run it lists exists.
4. `r2` (as the R2 account): each listed run's served files → R2, checked there, then the manifest. A second runnable
   of the publish job's one task (one provisioning, not two), unless the R2 account is another service account; its
   own job when the manifest already exists (a rerun).
5. `prune` (local): every earlier scan's `state/` in the scratch bucket, once `D`'s is complete (parallel deletes).

The base+runs reconstruction is exact, version for version, with a full rebuild through `D`
(`test_interval_append.py`): the change keys are `build`'s, `wts` is carried as `build` and `fold` carry it (a
version opened by a read-day change alone keeps its path version's; a slice piece opened by its path's change
alone keeps its slice version's), and the open states carry each path's version start (`pvf`), where the
slice pieces split.

    dt-cloud interval-store append [-c] [-n] SCAN_ID   # the chain, profile-driven ($INTERVAL_STORE_PROFILE / -P)
    python -m dt_cloud.interval_append ranges|publish|r2 …   # the Batch tasks' stages
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from time import monotonic

import pyarrow as pa
from click import IntRange, argument, command, group, option

from . import interval_store as ist
from . import static_names as sn
from .static_append import COMPACT_LEVEL, push_run, run_key
from .static_names import OPEN, U64, err, q, read_json, upload_tree, write_sorted
from .static_profile import Profile, from_mapping

PREFIX = ist.PREFIX
#: The served sorts a run carries: what the Worker reads (`R2_SERVED` less the standalone `reads` sort and the
#: superseded per-slice `slices-bysize`).
RUN_SORTS = ("path", "bysize", "slices", "slices-bytotal", "slices-bysize-user")
#: Cost-attribution `component` of every job the append submits (`cost_labels`).
COMPONENT = "interval-store"
#: Exit status: the scan is not published yet, or is not the next one to append.
NOT_NEXT = 3

PVF = pa.field("pvf", pa.int64(), nullable=False)
OP = pa.field("op", pa.int8(), nullable=False)
#: Open state per table (scratch): the served rows, `usr` '' where no owner is named (a join key), and for `pvl` and
#: `svt` the start of the path's current path version (`pvf`: a slice piece splits there).
STATE_SCHEMA = {
    "pvl": ist.PVL_SCHEMA.append(PVF),
    "sv": ist.SV_SCHEMA,
    "svt": ist.SVT_SCHEMA.append(PVF),
}
#: A run's delta files: the state's columns and `op` (1 opened, −1 a close record).
DELTA_SCHEMA = {k: s.append(OP) for k, s in STATE_SCHEMA.items()}
#: Each table's version identity (a version's rows across tiers share it).
IDENT = {"pvl": ["depth", "path", "vf"], "sv": ["depth", "path", "usr", "vf"], "svt": ["depth", "path", "usr", "vf"]}
TABLES = tuple(STATE_SCHEMA)
#: The version tables' change columns and the carried ones (`build`'s, `fold`'s): `dr` is computed from the stored
#: `wts`/`wb` (constant within a version), `pvf` rides along to cut the slice pieces.
PVL_STATE = [*ist.CHANGE_COLS, "last_read", "wts", "pvf"]
SV_STATE = ist.SV_STATE
SVT_STATE = [*ist.SV_CHANGE, "tot", "pvf", "wts"]
DR = "CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE 0 END::DOUBLE"
#: What a reconstruction check hashes per table (`interval_store`'s hashes, `last_read` in the path's).
HASH = {
    "pvl": "hash(depth, path, kind, size, n_files, n_children, n_desc, mtime, dr, wb, c2, c3, c4, us, last_read)",
    "sv": ist.SV_HASH,
    "svt": f"hash({ist.SV_HASH[5:-1]}, tot)",
}


# ── The open versions ──────────────────────────────────────────────────────


def base_state_sql(root: str, name: str) -> dict[str, str]:
    """A key range's open versions from a base generation's range files under `root` (`pvl/`, `pv/`, `sv/`, `svt/`
    `<name>.parquet`): `STATE_SCHEMA` relations. `pvf` is the open path version's `vf` (`pv/`)."""
    rd = lambda sub: f"read_parquet({q(f'{root}/{sub}/{name}.parquet')})"  # noqa: E731
    pv = f"(SELECT depth, path, vf AS pvf FROM {rd('pv')} WHERE vt = {OPEN})"
    pvl_cols = ", ".join(f"l.{c}" for c in ist.PVL_SCHEMA.names)
    svt_cols = ", ".join("coalesce(s.usr, '') AS usr" if c == "usr" else f"s.{c}" for c in ist.SVT_SCHEMA.names)
    sv_cols = ", ".join("coalesce(usr, '') AS usr" if c == "usr" else c for c in ist.SV_SCHEMA.names)
    return {
        "pvl": f"SELECT {pvl_cols}, p.pvf FROM {rd('pvl')} l JOIN {pv} p ON p.depth = l.depth AND p.path = l.path WHERE l.vt = {OPEN}",
        "sv": f"SELECT {sv_cols} FROM {rd('sv')} WHERE vt = {OPEN}",
        "svt": f"SELECT {svt_cols}, p.pvf FROM {rd('svt')} s JOIN {pv} p ON p.depth = s.depth AND p.path = s.path WHERE s.vt = {OPEN}",
    }


def run_state_sql(root: str, name: str) -> dict[str, str]:
    """A key range's open versions after a scan, from its `state/<D>/` dir `root`."""
    return {t: f"SELECT * FROM read_parquet({q(f'{root}/{t}/{name}.parquet')})" for t in TABLES}


def _reconstructs(con, table: str, new: str, kind: str) -> tuple[list[int], list[int]]:
    """`[count, Σ hash]` of the open versions in `table` and of the scan's rows `new` (equal iff the open state is
    the scan exactly)."""
    h = HASH[kind]

    def dig(rel: str) -> list[int]:
        n, s = con.execute(f"SELECT count(*), coalesce(sum({h}::HUGEINT), 0) FROM ({rel})").fetchone()
        return [int(n), int(s) % U64]

    return dig(f"SELECT * REPLACE ({DR} AS dr) FROM {table} WHERE vt = {OPEN}"), dig(new)


def append_range(con, prev: dict[str, str], scan: dict, r: dict, name: str, out: Path, *, bucket: str, mount: str | None) -> dict:
    """Append `scan` to one key range's open versions (`prev`: `STATE_SCHEMA` relations per table). Writes under
    `out` each table's delta `<t>/<name>.parquet` (`DELTA_SCHEMA`, sorted by identity then `op`) and next open state
    `state/<t>/<name>.parquet`. Raises unless the next open state is the scan's rows exactly."""
    from pyrmts.intervals import append_intervals

    t0 = monotonic()
    D = int(scan["ts"])
    src = sn._src(bucket, scan["src"], mount)
    preds = sn.range_preds(r)
    v = scan.get("version")
    con.execute(f"CREATE OR REPLACE TABLE np AS {ist.path_rows_sql(con, src, preds, v)}")
    con.execute(f"CREATE OR REPLACE TABLE ns AS {ist.slice_rows_sql(con, src, preds, v)}")
    for t in TABLES:
        con.execute(f"CREATE OR REPLACE TABLE p_{t} AS SELECT *, {DR} AS dr FROM ({prev[t]})")
    # Each path's version start after D: its open one's while the change columns hold, else D.
    same = " AND ".join(f"o.{c} IS NOT DISTINCT FROM n.{c}" for c in ist.CHANGE_COLS)
    con.execute(f"""CREATE OR REPLACE TABLE npf AS SELECT n.*, CASE WHEN o.path IS NOT NULL AND {same} THEN o.pvf ELSE {D} END::BIGINT AS pvf
        FROM np n LEFT JOIN p_pvl o ON o.depth = n.depth AND o.path = n.path""")
    doc: dict = {"range": name, "scan": scan["id"]}
    key2 = ["depth", "path"]
    key3 = ["depth", "path", "usr"]
    pvl_new = f"SELECT {', '.join([*key2, *ist.CHANGE_COLS, 'last_read', 'wts', 'pvf'])} FROM npf"
    sv_new = f"SELECT {', '.join([*key3, *SV_STATE])} FROM ns"
    svt_new = f"""SELECT {', '.join(f's.{c}' for c in [*key3, *ist.SV_CHANGE])}, p.size AS tot, p.pvf, s.wts
        FROM ns s JOIN npf p ON p.depth = s.depth AND p.path = s.path"""
    plan = {
        "pvl": (pvl_new, key2, PVL_STATE, {"wts": "first", "pvf": "first"}),
        "sv": (sv_new, key3, SV_STATE, ist.CARRIED),
        "svt": (svt_new, key3, SVT_STATE, ist.CARRIED),
    }
    for t, (new, key, state, carried) in plan.items():
        n_open, n_close = append_intervals(con, f"SELECT * FROM p_{t}", new, key, state, D, OPEN, carried=carried, out=f"o_{t}")
        on = " AND ".join(f"o_{t}.{c} = c.{c}" for c in key)
        if t == "pvl":
            # A version opened by its read day alone continues its path version: `fold` gives it that version's `wts`.
            con.execute(f"""UPDATE o_pvl SET wts = c.wts FROM (SELECT depth, path, wts FROM o_pvl WHERE vt = {D}) c
                WHERE o_pvl.vf = {D} AND o_pvl.pvf < {D} AND {on}""")
        elif t == "svt":
            # A piece opened by its path's change alone continues its slice version: `fold -S` gives it that one's `wts`.
            eq = " AND ".join(f"o_svt.{c} IS NOT DISTINCT FROM c.{c}" for c in ist.SV_CHANGE)
            con.execute(f"""UPDATE o_svt SET wts = c.wts FROM (SELECT * FROM o_svt WHERE vt = {D}) c
                WHERE o_svt.vf = {D} AND {on} AND {eq}""")
        schema = DELTA_SCHEMA[t]
        cols = ", ".join(schema.names[:-1])
        order = ", ".join([*IDENT[t], "op"])
        dict_cols = [c for c in ("kind", "us", "usr") if c in schema.names]
        n_delta = write_sorted(sn._batches(con, f"""SELECT {cols}, 1::TINYINT AS op FROM o_{t} WHERE vf = {D}
                UNION ALL SELECT {cols}, -1::TINYINT AS op FROM o_{t} WHERE vt = {D} ORDER BY {order}"""),
                               out / t / f"{name}.parquet", schema, ist.RANGE_RG, dictionary=dict_cols)
        n_state = write_sorted(sn._batches(con, f"SELECT {cols} FROM o_{t} WHERE vt = {OPEN} ORDER BY {', '.join(IDENT[t])}"),
                               out / "state" / t / f"{name}.parquet", STATE_SCHEMA[t], ist.RANGE_RG, dictionary=dict_cols)
        got, want = _reconstructs(con, f"o_{t}", {"pvl": "SELECT * FROM np", "sv": "SELECT * FROM ns",
                                                   "svt": "SELECT s.*, p.size AS tot FROM ns s JOIN np p ON p.depth = s.depth AND p.path = s.path"}[t], t)
        if got != want:
            raise RuntimeError(f"append {name} {scan['id']} {t}: the open versions ({got[0]:,} rows) are not the scan's ({want[0]:,})")
        doc[t] = {"opened": n_open, "closed": n_close, "delta": n_delta, "open": n_state}
        con.execute(f"DROP TABLE o_{t}; DROP TABLE p_{t}")
    con.execute("DROP TABLE np; DROP TABLE ns; DROP TABLE npf")
    doc["s"] = round(monotonic() - t0, 1)
    err(f"append {name} {scan['id']}: " + ", ".join(f"{t} +{doc[t]['opened']:,} −{doc[t]['closed']:,}" for t in TABLES) + f" in {doc['s']}s")
    return doc


# ── A run's served sorts, and merges ───────────────────────────────────────


def delta_relation(root: str, t: str, served: bool = True) -> str:
    """A run's delta files of table `t` under `root` as one relation; `served`: in the served sorts' columns
    (`interval_store.SUB_SCHEMA`, `usr` NULL where no owner is named)."""
    src = f"read_parquet({q(f'{root}/{t}/r*.parquet')})"
    if not served:
        return src
    cols = ist.SUB_SCHEMA[t].names
    return f"(SELECT {', '.join('nullif(usr, ' + q('') + ') AS usr' if c == 'usr' else c for c in cols)} FROM {src})"


def cut_run(con, root: str, out: Path, *, rg_rows: int = ist.SERVED_RG) -> dict:
    """A run's served sorts (`RUN_SORTS`) from its delta files under `root` → `out/<sort>.parquet` + `.groups.parquet`
    + `<sort>.json`. Open versions then close records (`seg_sql` without stamps: segment 0, then 1)."""
    docs = {}
    for sort in RUN_SORTS:
        sub, _, _ = ist.SORTS[sort]
        t0 = monotonic()
        doc = ist.write_served(con, delta_relation(root, sub), sort, out / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=rg_rows)
        doc["s"] = round(monotonic() - t0, 1)
        (out / f"{sort}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
        docs[sort] = doc
    return docs


def merge_range(inputs: list[str], name: str, out: Path) -> dict:
    """One key range of runs (`inputs`: their roots, oldest first) merged per table: rows of one version are one
    (`IDENT`), the smallest `vt` (a close record over the row it closes), `op` 1 if any input opened it."""
    from pyrmts.runs import merge_parquets

    doc = {}
    for t in TABLES:
        schema = DELTA_SCHEMA[t]
        stream = merge_parquets([f"{r}/{t}/{name}.parquet" for r in inputs], [*IDENT[t], "op"], identity=IDENT[t], reduce={"vt": "min", "op": "max"})
        doc[t] = write_sorted(stream, out / t / f"{name}.parquet", schema, ist.RANGE_RG,
                              dictionary=[c for c in ("kind", "us", "usr") if c in schema.names])
    return doc


#: What every run a manifest lists holds (the reader's files and the run's record), checked before it is written.
RUN_FILES = ("meta.json", *(f"served/{s}{x}" for s in RUN_SORTS for x in (".parquet", ".groups.parquet")))


def missing_files(runs: list[dict], exists: Callable[[str], bool]) -> list[str]:
    return [f"{r['key']}/{f}" for r in runs for f in RUN_FILES if not exists(f"{r['key']}/{f}")]


def manifest(gen: str, base: dict, runs: list[dict]) -> dict:
    """`manifests/<D>.json`: the base's scans and the runs (oldest first) with each run scan's epoch (`stamps`, what
    a reader tests `vf ≤ D < vt` at)."""
    scans = [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]
    stamps = {s: ts for r in runs for s, ts in r["stamps"].items()}
    return {"gen": gen, "date": scans[-1], "base_scans": len(base["scans"]), "scans": scans, "stamps": dict(sorted(stamps.items())),
            "runs": [{k: r[k] for k in ("key", "first", "last", "level", "scans", "rows", "bytes") if k in r} for r in runs]}


def run_meta(gen: str, run: dict, stamps: dict[str, int], docs: dict) -> dict:
    return {"gen": gen, "key": run["key"], "first": run["first"], "last": run["last"], "level": run["level"], "scans": run["scans"],
            "stamps": stamps, "rows": sum(d["rows"] for s, d in docs.items() if s == "path"),
            "bytes": sum(d["bytes"] + d["groups_bytes"] for d in docs.values()), "sorts": docs}


# ── Key ranges → tasks ─────────────────────────────────────────────────────


def assign_ranges(k: int, tasks: int, counts: list[int] | None) -> list[list[int]]:
    """Each of `tasks` tasks' key ranges (of `k`), ascending. With `counts` (each range's open rows after the previous
    scan, its cost): longest first, each to the least-loaded task (ties: the lower task), so the big ranges don't pile
    up in one task as contiguous blocks do. Without: interleaved (task `t`: `t`, `t + tasks`, …). Deterministic: every
    task computes the same plan and runs its own row of it."""
    if counts is None:
        return [list(range(t, k, tasks)) for t in range(tasks)]
    if len(counts) != k:
        raise ValueError(f"{len(counts)} counts for {k} ranges")
    load = [0] * tasks
    out: list[list[int]] = [[] for _ in range(tasks)]
    for i in sorted(range(k), key=lambda i: (-counts[i], i)):
        t = min(range(tasks), key=lambda t: (load[t], t))
        out[t].append(i)
        load[t] += counts[i]
    return [sorted(o) for o in out]


def open_counts(docs: dict[str, dict], k: int) -> list[int] | None:
    """Each range's open rows (Σ over `TABLES`) from a scan's `ranges/r####.json` docs (by name), or None unless all `k`
    are there with every table's count (a run before the counts, or the base: no docs)."""
    out = []
    for i in range(k):
        d = docs.get(f"r{i:04d}")
        if d is None or any(not isinstance(d.get(t), dict) or "open" not in d[t] for t in TABLES):
            return None
        out.append(sum(int(d[t]["open"]) for t in TABLES))
    return out


def prev_open_counts(gcs, bucket: str, prefix: str, prev: str, k: int, *, workers: int = 8) -> list[int] | None:
    """`open_counts` of the previous scan's run (`<prefix>/deltas/<prev>/ranges/`), read in parallel."""
    root = f"{prefix}/{run_key(prev, prev)}/ranges/"
    b = gcs.bucket(bucket)
    names = sorted(x.name for x in gcs.list_blobs(bucket, prefix=root) if x.name.endswith(".json"))
    if len(names) < k:
        return None
    with ThreadPoolExecutor(workers) as ex:
        docs = dict(zip((Path(n).stem for n in names), ex.map(lambda n: json.loads(b.blob(n).download_as_bytes()), names)))
    return open_counts(docs, k)


# ── The open-version state: the newest complete scan only ─────────────────


class StateIncomplete(Exception):
    """`prune` refused: the scan's state is not complete (or its run not published), so nothing is deleted."""


def prune_plan(objects: list[tuple[str, int]], prefix: str, k: int, published: bool, scan: str) -> dict:
    """What `prune` deletes, from the scratch bucket's `(name, size)` listing under `<prefix>/state/`: every scan's
    state before `scan`, once `scan`'s is complete (every table's `k` range files) and its manifest `published`.
    Raises `StateIncomplete` otherwise."""
    root = f"{prefix}/state/"
    scans: dict[str, dict] = {}
    for name, size in objects:
        if not name.startswith(root):
            raise ValueError(f"{name}: not under {root}")
        sid, _, rest = name[len(root):].partition("/")
        if not sn.SCAN_ID.fullmatch(sid):
            raise ValueError(f"{name}: {sid!r} is not a scan id")
        d = scans.setdefault(sid, {"names": [], "bytes": 0, "files": {t: set() for t in TABLES}})
        d["names"].append(name)
        d["bytes"] += size
        t, _, file = rest.partition("/")
        if t in TABLES and file.endswith(".parquet"):
            d["files"][t].add(file.removesuffix(".parquet"))
    want = {f"r{i:04d}" for i in range(k)}
    cur = scans.get(scan, {"files": {t: set() for t in TABLES}})
    missing = {t: len(want - cur["files"][t]) for t in TABLES}
    if any(missing.values()) or not published:
        why = [f"{n} of {k} ranges without {t}" for t, n in missing.items() if n] + ([] if published else ["no manifest"])
        raise StateIncomplete(f"state/{scan} incomplete: {', '.join(why)}")
    drop = sorted(s for s in scans if s < scan)
    return {"scan": scan, "keep": sorted(s for s in scans if s >= scan),
            "delete": [{"scan": s, "objects": len(scans[s]["names"]), "bytes": scans[s]["bytes"]} for s in drop],
            "names": [n for s in drop for n in sorted(scans[s]["names"])]}


# ── The deployment's settings ──────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    """The append's settings: a `static_profile.Profile` (buckets, layouts, Batch, R2) from the interval profile's
    `append` section, plus the generation whose range files hold the base's versions (`ranges_gen`: the served
    generation may be a re-cut of another's ranges)."""
    p: Profile
    ranges_gen: str


#: Env overrides of an interval profile's `append` fields (a staged source tree, a job image, the R2 endpoint).
ENV = {"src": "INTERVAL_STORE_SRC", "image": "INTERVAL_STORE_IMAGE", "r2_endpoint": "R2_ENDPOINT", "project": "GCP_PROJECT",
       "gen": "INTERVAL_STORE_GEN", "machine": "INTERVAL_STORE_MACHINE"}


def load_config(profile: str | None, environ: dict[str, str] | None = None) -> Config:
    """The interval profile's `append` section (`interval_profiles/<name>.json`, or a path; `$INTERVAL_STORE_PROFILE`)
    with `ENV` over it; every field a run needs is checked (`need`)."""
    env = os.environ if environ is None else environ
    doc = ist.load_profile(profile)
    a = dict(doc.get("append") or {})
    if not a:
        raise SystemExit("interval store: the profile has no `append` section")
    ranges_gen = a.pop("ranges_gen", None)
    p = from_mapping({"name": doc.get("name", profile or ""), "bucket": doc["bucket"], **a})
    over = {}
    for k, var in ENV.items():
        raw = (env.get(var) or "").strip()
        if raw:
            over[k] = tuple(s.strip() for s in raw.split(",") if s.strip()) if k == "src" else raw
    p = replace(p, **over)
    for f in ("gen", "bucket", "scratch", "layouts", "region", "image", "sa", "r2_bucket"):
        if getattr(p, f) in (None, "", ()):
            raise SystemExit(f"interval store: no {f} in the profile's `append` section" + (f" (or ${ENV[f]})" if f in ENV else ""))
    return Config(p, ranges_gen or p.gen)


# ── Batch ──────────────────────────────────────────────────────────────────


def task_command(cfg: Config, stage: str, args: list[str], *, mount: bool = True) -> str:
    """A stage task's shell command: `python -m dt_cloud.interval_append <stage> <args> -b … -g … [-m /gcs/<bucket>]`."""
    from .static_runner import task_command as tc

    p = cfg.p
    common = ["-b", p.bucket, "-g", p.gen, "-R", cfg.ranges_gen, "-S", p.scratch]
    return tc(p, "interval_append", [stage, *args, *common], mount=mount)


def job_id(stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`iv-<stage>-<scan>-<hhmmss>` (Batch ids: lowercase letters, digits and hyphens)."""
    t = (now or datetime.now(timezone.utc)).strftime("%H%M%S")
    return f"iv-{stage}-{scan_id.lower().replace('t', '-')}-{t}"


def job_spec(cfg: Config, name: str, tasks: int, commands: list[str], *, stage: str, r2: bool = False, machine: str | None = None,
             ssd_gb: int | None = None) -> dict:
    from .static_runner import job_spec as js

    return js(cfg.p, name, tasks, commands, stage=stage, r2=r2, machine=machine, ssd_gb=ssd_gb, purpose="interval-store", component=COMPONENT)


# ── The chain ──────────────────────────────────────────────────────────────


@dataclass
class Runner:
    """The chain over injectable effects (tests pass fakes), as `static_runner.Runner`: `exists(key)` / `count(prefix,
    suffix)` / `read_json(key)` / `list_keys(prefix)` on the data bucket (keys relative to it), `published(layouts,
    start)` the scan ids under the profile's layouts, `run_job(name, spec)`, `prepare(scan_id)` and `prune(scan_id)`."""
    cfg: Config
    exists: Callable[[str], bool]
    count: Callable[[str, str], int]
    read_json: Callable[[str], dict]
    list_keys: Callable[[str], list[str]]
    published: Callable[[tuple[str, ...], str], list[str]]
    run_job: Callable[[str, dict], None]
    prepare: Callable[[str], None]
    prune: Callable[[str], None]
    log: Callable[[str], None] = err
    dry_run: bool = False
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    timings: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        self.timings = {}

    @property
    def root(self) -> str:
        return f"{PREFIX}/{self.cfg.p.gen}"

    def manifests(self) -> list[str]:
        return sorted(k for k in self.list_keys(f"{self.root}/manifests/") if k.endswith(".json"))

    def have(self) -> list[str]:
        """The generation's scans: the base's and the newest manifest's runs'."""
        base = self.read_json(f"{self.root}/scans.json")
        keys = self.manifests()
        runs = self.read_json(keys[-1])["runs"] if keys else []
        return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]

    def run(self, scan_id: str, catch_up: bool = False) -> list[str]:
        """Append `scan_id` (and with `catch_up` every earlier pending scan, in order). Returns the scans appended."""
        from .static_runner import pending_scans

        have = self.have()
        todo = pending_scans(have, self.published(self.cfg.p.layouts, have[-1]), scan_id, catch_up)
        if not todo:
            self.log(f"{scan_id}: already appended; the R2 copy and prune only")
            if scan_id == have[-1]:
                self.r2(scan_id)
                self.stage(f"{scan_id} prune", lambda: self.prune(scan_id))
            return []
        if len(todo) > 1:
            self.log(f"catching up {len(todo)} scans: {', '.join(todo)}")
        for s in todo:
            self.one(s)
        return todo

    def stage(self, name: str, fn: Callable[[], None]) -> None:
        if self.dry_run:
            self.log(f"{name}: would run")
            return
        t = monotonic()
        self.log(f"{name}: start")
        fn()
        self.timings[name] = round(monotonic() - t, 1)
        self.log(f"{name}: done in {self.timings[name]:.0f}s")

    def job(self, stage: str, scan_id: str, tasks: int, args: list[str], **kw) -> tuple[str, dict]:
        name = job_id(stage, scan_id, self.now())
        cmd = task_command(self.cfg, stage, args, mount=kw.pop("mount", True))
        return name, job_spec(self.cfg, name, tasks, [cmd], stage=stage, **kw)

    def one(self, d: str) -> None:
        run = f"{self.root}/deltas/{d}"
        self.log(f"{d}: interval store {self.cfg.p.gen}")
        # 1. prepare (local): the scan's `path` sort, pinned (checks it is the next one).
        if self.exists(f"{run}/scans.json"):
            self.log(f"{d} prepare: done")
        else:
            self.stage(f"{d} prepare", lambda: self.prepare(d))
        # 2. ranges: every key range, `append_tasks` tasks.
        k = self.read_json(f"{self.cfg_ranges_json()}")["k"]
        n = self.count(f"{run}/ranges/", ".json")
        if n >= k:
            self.log(f"{d} ranges: done ({n}/{k})")
        else:
            per = ceil(k / self.cfg.p.append_tasks)
            # DuckDB sized to the machine (¾ of its memory, every vCPU).
            vcpus = int(self.cfg.p.machine.rsplit("-", 1)[-1])
            mem = ["-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus)]
            name, spec = self.job("ranges", d, ceil(k / per), ["-d", d, "-n", str(per), *mem])
            self.stage(f"{d} ranges ({n}/{k})", lambda: self.run_job(name, spec))
        # 3. publish: the run's cut, the counter's merges, then `manifests/<d>.json` (written once); 4. R2: the runs the
        # manifest lists, checked, then the manifest — one task's two runnables, so one provisioning.
        if self.exists(f"{self.root}/manifests/{d}.json"):
            self.log(f"{d} publish: done")
            self.r2(d)
        elif self.cfg.p.r2_sa in (None, "", self.cfg.p.sa):
            name, spec = self.publish_job(d, r2=True)
            self.stage(f"{d} publish+r2", lambda: self.run_job(name, spec))
        else:
            # The R2 account is another service account: a job runs as one, so the copy is its own job.
            name, spec = self.publish_job(d)
            self.stage(f"{d} publish", lambda: self.run_job(name, spec))
            self.r2(d)
        # 5. prune: only the newest complete open-version state is kept.
        self.stage(f"{d} prune", lambda: self.prune(d))

    def cfg_ranges_json(self) -> str:
        return f"{PREFIX}/{self.cfg.ranges_gen}/ranges.json"

    def publish_job(self, d: str, r2: bool = False) -> tuple[str, dict]:
        """The publish job; with `r2`, as the R2 account, its task's second runnable the R2 copy (Batch runs a task's
        runnables in order and stops at a failed one, so the copy only follows a written manifest). A retried task's
        publish skips a manifest it already wrote (`-s`), and the copy skips objects already on R2."""
        vcpus = int(self.cfg.p.machine.rsplit("-", 1)[-1])
        args = ["-d", d, "-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus), *(["-s"] if r2 else [])]
        name, spec = self.job("publish", d, 1, args, r2=r2)
        if r2:
            ts = spec["taskGroups"][0]["taskSpec"]
            copy = deepcopy(ts["runnables"][0])
            copy["container"]["commands"] = ["-c", task_command(self.cfg, "r2", ["-d", d], mount=False)]
            ts["runnables"].append(copy)
        return name, spec

    def r2(self, d: str) -> None:
        if self.dry_run:
            self.log(f"{d} r2: would copy the manifest's runs, check them, then the manifest")
            return
        name = job_id("r2", d, self.now())
        cmd = task_command(self.cfg, "r2", ["-d", d], mount=False)
        spec = job_spec(self.cfg, name, 1, [cmd], stage="r2", r2=True, machine="n2-highmem-4", ssd_gb=375)
        self.stage(f"{d} r2", lambda: self.run_job(name, spec))


def gcs_runner(cfg: Config, *, dry_run: bool = False) -> Runner:
    """`Runner` over the real data bucket, Batch, and this module's `prepare` / `prune`."""
    from google.cloud import storage

    from .static_runner import BatchRunner, NotNext

    p = cfg.p
    client = storage.Client(project=p.project)
    b = client.bucket(p.bucket)

    def published(layouts, start):
        return [s["id"] for s in sn.list_scans(p.bucket, layouts=layouts, start=start)["scans"]]

    def prepare(d):
        try:
            prepare_scan(p.bucket, p.gen, d, p.layouts)
        except SystemExit as e:
            raise NotNext(str(e)) from e

    def prune(d):
        k = read_json(f"gs://{p.bucket}/{PREFIX}/{cfg.ranges_gen}/ranges.json")["k"]
        doc = prune_state(client, p.gen, d, k, bucket=p.bucket, scratch=p.scratch, dry_run=dry_run)
        err(json.dumps({k_: v for k_, v in doc.items() if k_ != "delete"} | {"deleted_scans": [x["scan"] for x in doc["delete"]]}))

    return Runner(
        cfg=cfg,
        exists=lambda key: b.blob(key).exists(),
        count=lambda prefix, suffix: sum(1 for x in client.list_blobs(p.bucket, prefix=prefix) if x.name.endswith(suffix)),
        read_json=lambda key: read_json(f"gs://{p.bucket}/{key}"),
        list_keys=lambda prefix: [x.name for x in client.list_blobs(p.bucket, prefix=prefix)],
        published=published,
        # Its jobs run a minute or a few: poll at most every 20 s (the default backoff to 120 s added ~3 min per scan).
        run_job=BatchRunner(p, err, delay=10, max_delay=20),
        prepare=prepare,
        prune=prune,
        dry_run=dry_run,
    )


# ── GCS-side stages (local, or a Batch task) ───────────────────────────────


def _gcs():
    from google.cloud import storage

    return storage.Client()


def _latest_manifest(bucket: str, gen: str, before: str | None = None) -> dict | None:
    keys = sorted(b.name for b in _gcs().list_blobs(bucket, prefix=f"{PREFIX}/{gen}/manifests/") if b.name.endswith(".json"))
    if before:
        keys = [k for k in keys if Path(k).stem < before]
    return read_json(f"gs://{bucket}/{keys[-1]}") if keys else None


def _state(bucket: str, gen: str, scan: str) -> tuple[dict, list[dict]]:
    """The generation's base `scans.json` and the runs live before `scan` (the newest earlier manifest's)."""
    base = read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json")
    m = _latest_manifest(bucket, gen, before=scan)
    return base, (m["runs"] if m else [])


def _scans_of(base: dict, runs: list[dict]) -> list[str]:
    return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]


def prepare_scan(bucket: str, gen: str, scan: str, layouts: tuple[str, ...]) -> dict:
    """Pin `scan`'s newest `path` sort (GCS generation, size, md5, crc32c, source format) as `deltas/<scan>/scans.json`,
    refusing (SystemExit) unless it is the next scan after the base and the live runs."""
    base, runs = _state(bucket, gen, scan)
    have = _scans_of(base, runs)
    found = sn.list_scans(bucket, layouts=layouts, start=have[-1])["scans"]
    nxt = [s for s in found if s["id"] > have[-1]]
    if not nxt or nxt[0]["id"] != scan:
        raise SystemExit(f"the next scan after {have[-1]} is {nxt[0]['id'] if nxt else 'none'}, not {scan}")
    s = nxt[0]
    if s["ts"] <= max(x["ts"] for x in base["scans"]):
        raise SystemExit(f"{scan}: its stamp {s['ts']} is not past the generation's")
    s["version"] = sn._version_from_footer(_gcs().bucket(bucket), s["src"])
    doc = {"bucket": bucket, "scans": [s]}
    _gcs().bucket(bucket).blob(f"{PREFIX}/{gen}/{run_key(scan, scan)}/scans.json").upload_from_string(json.dumps(doc, indent=1) + "\n",
                                                                                                    if_generation_match=0)
    err(json.dumps(doc))
    return doc


def delete_objects(bucket, names: list[str], *, workers: int = 8) -> int:
    """Delete `names` from `bucket` (a `google.cloud.storage.Bucket`) over `workers` threads (one request each; ≤ the
    client's 10 pooled connections). One already gone is skipped; any other error raises. Returns the objects deleted."""
    from google.api_core.exceptions import NotFound

    def one(n: str) -> int:
        try:
            bucket.blob(n).delete()
        except NotFound:
            return 0
        return 1

    with ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(one, names))


def prune_state(gcs, gen: str, scan: str, k: int, *, bucket: str, scratch: str, dry_run: bool = False, workers: int = 8) -> dict:
    """Delete every `state/<prev>/` (prev < `scan`) of generation `gen` in the scratch bucket (nowhere else), once
    `scan`'s state is complete and its manifest published (`prune_plan`), `workers` deletes at a time. Idempotent."""
    prefix = f"{PREFIX}/{gen}"
    objects = [(b.name, int(b.size or 0)) for b in gcs.list_blobs(scratch, prefix=f"{prefix}/state/")]
    published = gcs.bucket(bucket).blob(f"{prefix}/manifests/{scan}.json").exists()
    plan = prune_plan(objects, prefix, k, published, scan)
    names = plan.pop("names")
    if not dry_run and names:
        delete_objects(gcs.bucket(scratch), names, workers=workers)
    return {**plan, "deleted": 0 if dry_run else len(names)}


# ── CLI ────────────────────────────────────────────────────────────────────


@group("runs")
def cli() -> None:
    """The interval store's per-scan runs beside a base generation (specs/interval-store.md §2.7): the chain's stages."""


def stage_options(f):
    for o in reversed([
        option("-b", "--bucket", required=True, help="Data bucket"),
        option("-g", "--gen", required=True, help="Base generation (`interval-store/<gen>/`: its `scans.json`, the runs, the manifests)"),
        option("-R", "--ranges-gen", required=True, help="The generation holding the base's range files and `ranges.json`"),
        option("-S", "--scratch", required=True, help="Bucket for the open versions (`state/<D>/`)"),
    ]):
        f = o(f)
    return f


@cli.command("ranges")
@stage_options
@option("-d", "--scan", required=True, help="The scan to append (its run's `scans.json`, from `prepare`)")
@option("-f", "--force", is_flag=True, help="Redo ranges already done")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the scratch bucket beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def ranges_cmd(bucket, gen, ranges_gen, scratch, scan, force, index, mount, mem, per_task, out, threads, tmp) -> None:
    """Append the scan to key ranges' open versions: `deltas/<D>/{pvl,sv,svt}/r####.parquet` and `ranges/r####.json`
    (data bucket; the marker last), `state/<D>/{pvl,sv,svt}/r####.parquet` (scratch). The open versions come from the
    previous scan's state, else (the first run) the base's range files."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(scan, scan)}"
    doc_scan = read_json(f"gs://{bucket}/{run}/scans.json")
    s = doc_scan["scans"][0]
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{ranges_gen}/ranges.json")
    base, runs = _state(bucket, gen, scan)
    prev = _scans_of(base, runs)[-1]
    smount = str(Path(mount).parent / scratch)
    t = sn._task(index)
    k = ranges["k"]
    # Balanced by the previous scan's open rows (a range's cost), else interleaved: tasks = the job's.
    counts = prev_open_counts(_gcs(), bucket, prefix, prev, k) if runs else None
    plan = assign_ranges(k, ceil(k / per_task), counts)
    todo = plan[t] if t < len(plan) else []
    err(f"ranges task {t}: {len(todo)} ranges" + (f", {sum(counts[i] for i in todo):,} open rows at {prev} (max task "
                                                  f"{max(sum(counts[i] for i in p) for p in plan):,})" if counts else ", interleaved"))
    b = _gcs().bucket(bucket)
    con = sn.connect(threads, mem, tmp)
    for i in todo:
        name = f"r{i:04d}"
        if not force and b.blob(f"{run}/ranges/{name}.json").exists():
            err(f"ranges {name}: done")
            continue
        state = (run_state_sql(f"{smount}/{prefix}/state/{prev}", name) if runs
                 else base_state_sql(f"{mount}/{PREFIX}/{ranges_gen}", name))
        outp = Path(out) / name
        shutil.rmtree(outp, ignore_errors=True)
        doc = append_range(con, state, s, ranges["ranges"][i], name, outp, bucket=doc_scan["bucket"], mount=mount)
        doc["prev"] = prev
        upload_tree(outp / "state", scratch, f"{prefix}/state/{scan}")
        for tb in TABLES:
            upload_tree(outp / tb, bucket, f"{run}/{tb}")
        shutil.rmtree(outp)
        b.blob(f"{run}/ranges/{name}.json").upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


def _cut_and_upload(con, bucket: str, prefix: str, run: dict, stamps: dict[str, int], src_root: str, tmp: Path, gen: str) -> dict:
    """Cut a run's served sorts from its delta files (`src_root`, a mounted dir) and upload them, then its `meta.json`."""
    outp = tmp / "served"
    shutil.rmtree(outp, ignore_errors=True)
    t0 = monotonic()
    docs = cut_run(con, src_root, outp)
    upload_tree(outp, bucket, f"{prefix}/{run['key']}/served")
    shutil.rmtree(outp)
    meta = run_meta(gen, run, stamps, docs)
    meta["cut_s"] = round(monotonic() - t0, 1)
    _gcs().bucket(bucket).blob(f"{prefix}/{run['key']}/meta.json").upload_from_string(json.dumps(meta, indent=1) + "\n")
    return meta


@cli.command("publish")
@stage_options
@option("-d", "--scan", required=True, help="The scan")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Print the merges and manifest; write nothing")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-s", "--skip-published", is_flag=True, help="Exit 0 when `manifests/<D>.json` exists (a retried publish+r2 task goes on to the copy)")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir")
def publish_cmd(bucket, gen, ranges_gen, scratch, scan, mount, mem, dry_run, threads, skip_published, tmp) -> None:
    """Cut the run's served sorts (`deltas/<D>/served/`, then its `meta.json`), carry the binary counter (each merge a
    new run dir: its deltas, its cut, its `meta.json`), then write `manifests/<D>.json` once, after checking every
    file of every run it lists. A level reaching `COMPACT_LEVEL` is reported: time for a new base generation."""
    prefix = f"{PREFIX}/{gen}"
    key = f"{prefix}/manifests/{scan}.json"
    b = _gcs().bucket(bucket)
    if skip_published and b.blob(key).exists():
        err(f"{key}: published")
        return
    base, runs = _state(bucket, gen, scan)
    if _scans_of(base, runs)[-1] >= scan:
        raise SystemExit(f"{scan} is not past the generation's newest scan {_scans_of(base, runs)[-1]}")
    if b.blob(key).exists():
        raise SystemExit(f"{key} exists: manifests are never rewritten")
    k = read_json(f"gs://{bucket}/{PREFIX}/{ranges_gen}/ranges.json")["k"]
    rk = run_key(scan, scan)
    done = sum(1 for _ in _gcs().list_blobs(bucket, prefix=f"{prefix}/{rk}/ranges/"))
    if done < k:
        raise SystemExit(f"{prefix}/{rk}: {done} of {k} ranges appended")
    s = read_json(f"gs://{bucket}/{prefix}/{rk}/scans.json")["scans"][0]
    new = {"key": rk, "first": scan, "last": scan, "scans": [scan], "level": 0}
    after, merges = push_run(runs, new)
    if dry_run:
        print(json.dumps({"merges": [[[r["key"] for r in ins], m["key"]] for ins, m in merges], "manifest": manifest(gen, base, [*runs, {**new, "stamps": {scan: s["ts"]}}])}, indent=1))
        return
    con = sn.connect(threads, mem, tmp)
    tmp = Path(tmp)
    metas: dict[str, dict] = {}

    def meta_of(r: dict) -> dict:
        if r["key"] not in metas:
            metas[r["key"]] = read_json(f"gs://{bucket}/{prefix}/{r['key']}/meta.json")
        return metas[r["key"]]

    if b.blob(f"{prefix}/{rk}/meta.json").exists():
        err(f"{rk}: cut")
    else:
        metas[rk] = _cut_and_upload(con, bucket, prefix, new, {scan: s["ts"]}, f"{mount}/{prefix}/{rk}", tmp, gen)
        err(f"{rk}: cut in {metas[rk]['cut_s']}s ({metas[rk]['bytes']:,} B)")
    for ins, m in merges:
        if b.blob(f"{prefix}/{m['key']}/meta.json").exists():
            err(f"{m['key']}: merged")
            continue
        # A merged run is written once: an earlier attempt may have left only keys this merge writes.
        t0 = monotonic()
        outp = tmp / "merge" / m["key"]
        shutil.rmtree(outp, ignore_errors=True)
        roots = [f"{mount}/{prefix}/{r['key']}" for r in ins]
        for i in range(k):
            merge_range(roots, f"r{i:04d}", outp)
        upload_tree(outp, bucket, f"{prefix}/{m['key']}")
        merged_s = round(monotonic() - t0, 1)
        stamps = {sid: ts for r in ins for sid, ts in meta_of(r)["stamps"].items()}
        meta = _cut_and_upload(con, bucket, prefix, m, stamps, str(outp), tmp, gen)
        meta["merge_s"] = merged_s
        metas[m["key"]] = meta
        shutil.rmtree(outp)
        err(f"{m['key']}: merged {[r['key'] for r in ins]} in {merged_s}s, cut in {meta['cut_s']}s")
    full = [{**r, "stamps": meta_of(r)["stamps"], "rows": meta_of(r)["rows"], "bytes": meta_of(r)["bytes"]} for r in after]
    doc = manifest(gen, base, full)
    if max(r["level"] for r in after) >= COMPACT_LEVEL:
        err(f"level {COMPACT_LEVEL} reached: compact into a new base generation")
    if missing := missing_files(after, lambda f: b.blob(f"{prefix}/{f}").exists()):
        raise SystemExit(f"not publishing {key}: listed runs lack {missing}")
    b.blob(key).upload_from_string(json.dumps(doc, indent=1) + "\n", if_generation_match=0)
    print(json.dumps(doc, indent=1))


@cli.command("r2")
@stage_options
@option("-d", "--scan", required=True, help="The published scan (`manifests/<D>.json`)")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
def r2_cmd(bucket, gen, ranges_gen, scratch, scan, workers) -> None:
    """Copy the served files of every run `manifests/<D>.json` lists GCS → R2 (objects already there with the same size
    and md5 skipped), check every one is there, then copy the manifest, last. R2 via `R2_ENDPOINT`, `R2_BUCKET` and
    AWS_* (or R2_*) keys."""
    from . import publish as pub

    prefix = f"{PREFIX}/{gen}"
    key = f"{prefix}/manifests/{scan}.json"
    runs = read_json(f"gs://{bucket}/{key}")["runs"]
    objs = [o for r in runs for o in pub.list_source(bucket, [f"{prefix}/{r['key']}/served/"])]
    s3, r2 = pub.r2_client(), pub.r2_bucket()
    t0 = monotonic()
    with ThreadPoolExecutor(workers) as ex:
        todo = [o for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]
        for o in ex.map(lambda o: (pub.copy_one(bucket, s3, r2, o), o)[1], todo):
            err(f"  → {o.key} ({o.size:,} B)")
        bad = [o.key for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]
    if bad:
        raise SystemExit(f"r2 {scan}: {len(bad)} of {len(objs)} served files not on R2 after the copy (e.g. {bad[0]}): manifest not copied")
    man = pub.list_source(bucket, [key])
    for o in man:
        if pub.should_copy(o, pub.head_dest(s3, r2, o.key)):
            pub.copy_one(bucket, s3, r2, o)
    print(json.dumps({"gen": gen, "scan": scan, "runs": [r["key"] for r in runs], "objects": len(objs), "copied": len(todo),
                      "bytes": sum(o.size for o in todo), "s": round(monotonic() - t0, 1)}))


@command("append")
@option("-c", "--catch-up", is_flag=True, help="Append every earlier published scan still pending first, in scan-id order")
@option("-n", "--dry-run", is_flag=True, help="Report each stage's state and what would run; submit and write nothing")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
@argument("scan_id")
def append_cmd(catch_up: bool, dry_run: bool, profile: str | None, scan_id: str) -> None:
    """Append SCAN_ID to the interval store: prepare → ranges → publish → R2 → prune, each stage skipped when its output
    exists. Exit 3 when SCAN_ID is not published yet, or an earlier published scan is pending (without -c)."""
    from .static_runner import NotNext

    cfg = load_config(profile)
    if not cfg.p.project:
        from .gcp import gcp_project

        cfg = replace(cfg, p=replace(cfg.p, project=gcp_project()))
    cfg.p.r2_env_secrets()
    runner = gcs_runner(cfg, dry_run=dry_run)
    t0 = monotonic()
    try:
        done = runner.run(scan_id, catch_up=catch_up)
    except NotNext as e:
        err(f"interval-store append {scan_id}: {e}")
        sys.exit(NOT_NEXT)
    except RuntimeError as e:
        err(f"interval-store append {scan_id}: {e}")
        sys.exit(1)
    print(json.dumps({"gen": cfg.p.gen, "scan": scan_id, "appended": done, "dry_run": dry_run, "s": round(monotonic() - t0, 1),
                      "stages": runner.timings}))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cli()
