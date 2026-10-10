"""Deferred carries for the static name index's runs (specs/static-append.md, "Deferred carries").

A scan's `publish` only adds its own level-0 run and writes `manifests/<id>.json`. The binary counter's carries run here,
apart from the scan's chain (`dt-cloud static-names runs merge`, or the `merge` stage `runs add` submits after R2, non-fatal):

1. **Plan** from the newest manifest (`plan_carries`): the counter replayed over its runs. Carries that chain at one
   step fold into one N-way merge (`[L1, L0, L0]` → one L2 from three inputs, not an L1 and then an L2), and no carry
   reaches `COMPACT_LEVEL` (that is a compaction: a new base generation).
2. **Merge** one planned run at a time into its own new dir (`build_merged_run`: shards, catalog, drill, names + anchors,
   each tier in its own process), uploaded with its `meta.json` last. A dir whose `meta.json` already says it holds
   the planned scans (an interrupted attempt that got that far) is reused, not rebuilt.
3. **Publish a revision**: `manifests/<id>.m<NNN>.json`, a new key beside the newest manifest `<id>.json` (`id` its newest
   scan), listing the same scans with the inputs replaced by the merged run. Revisions sort after their scan's
   `<id>.json` and before every later scan's, so every reader's "greatest key under `manifests/`" picks them up as is.
   Written with `if_generation_match=0` once every file of every run it lists exists; if a scan's `publish` lands
   meanwhile, the merge is rebased onto that newer manifest instead (`rebase`).

Nothing is deleted: superseded runs stay where older manifests list them. One merger at a time per generation: a lease
(`merge.lease.json` in the scratch bucket) held for the job's life, stale after `LEASE_S`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Protocol

from click import IntRange, group, option

from .static_names import PREFIX, connect, err
from .static_profile import data_bucket, scratch_bucket

#: Revision suffix width: `<id>.m001.json` … `<id>.m999.json` sort in revision order.
REV_DIGITS = 3
MANIFEST = re.compile(rf"(?P<scan>[^/]+?)(?:\.m(?P<rev>\d{{{REV_DIGITS}}}))?\.json")
#: The merge lease's lifetime: a Batch task's `maxRunDuration` (`static_runner.job_spec`), so a crashed holder's
#: lease never blocks the next merge for longer than its job could have run.
LEASE_S = 4 * 3600
#: The fraction of the machine's memory the concurrent tier merges' DuckDB limits sum to.
MEM_FRACTION = 0.75
#: Memory set aside per streaming (non-DuckDB) tier merge: `pyrmts.runs` holds a few batches per input.
STREAM_RESERVE = 4 << 30
#: Tiers merged by DuckDB (each gets a memory limit and threads); the others stream through `pyrmts.runs`.
DUCKDB_TIERS = ("drill", "anchors")


# ── Manifest names ─────────────────────────────────────────────────────────


def manifest_name(scan: str, rev: int = 0) -> str:
    """`<scan>.json` (a scan's `publish`), or `<scan>.m<NNN>.json` (a merge's revision of it)."""
    if not 0 <= rev < 10 ** REV_DIGITS:
        raise ValueError(f"manifest revision {rev}: outside 0..{10 ** REV_DIGITS - 1}")
    return f"{scan}.json" if rev == 0 else f"{scan}.m{rev:0{REV_DIGITS}d}.json"


def parse_manifest(name: str) -> tuple[str, int] | None:
    """`(scan, rev)` of a manifest's file name (`rev` 0 for a scan's own), or None for anything else."""
    from .scan_id import SCAN_ID

    m = MANIFEST.fullmatch(name)
    return (m["scan"], int(m["rev"] or 0)) if m and SCAN_ID.fullmatch(m["scan"]) else None


def manifest_keys(keys: Collection[str]) -> list[str]:
    """The manifests among generation-relative `keys` (`manifests/<name>`), oldest first: by key, which is by scan, then
    revision."""
    return sorted(k for k in keys if k.startswith("manifests/") and parse_manifest(k.removeprefix("manifests/")))


# ── The plan ───────────────────────────────────────────────────────────────


def plan_carries(runs: list[dict], drilled: Collection[str] = frozenset(), max_level: int | None = None) -> tuple[list[dict], list[tuple[list[dict], dict]]]:
    """The binary counter replayed over `runs` (oldest first): each pushed in turn, and while the two newest share a level
    (and both carry `drill/` or neither: `push_run`'s rule) they carry into one a level up. Carries that chain fold into
    one merge of every run they consumed. A carry never reaches `max_level` (default `COMPACT_LEVEL`: a compaction's
    job). Returns the runs after and the merges (`(inputs, output)`, oldest first; their inputs are disjoint)."""
    from .static_append import COMPACT_LEVEL, run_key

    top = COMPACT_LEVEL if max_level is None else max_level
    drilled = set(drilled)
    stack: list[tuple[dict, list[dict]]] = []
    for r in runs:
        stack.append((r, [r]))
        while len(stack) >= 2:
            (a, ia), (b, ib) = stack[-2], stack[-1]
            if a["level"] != b["level"] or a["level"] + 1 >= top or (a["key"] in drilled) != (b["key"] in drilled):
                break
            m = {"key": run_key(a["first"], b["last"]), "first": a["first"], "last": b["last"], "level": a["level"] + 1,
                 "scans": [*a["scans"], *b["scans"]]}
            if a["key"] in drilled:
                drilled.add(m["key"])
            stack[-2:] = [(m, ia + ib)]
    return [r for r, _ in stack], [(ins, r) for r, ins in stack if len(ins) > 1]


class Superseded(Exception):
    """A merge's inputs are no longer consecutive runs of the newest manifest: nothing to publish."""


def rebase(runs: list[dict], inputs: list[str], out: dict) -> list[dict] | None:
    """`runs` with the consecutive runs keyed `inputs` replaced by `out`; None when `out` is already listed. Raises
    `Superseded` when they aren't there consecutively."""
    keys = [r["key"] for r in runs]
    if out["key"] in keys:
        return None
    for i in range(len(keys) - len(inputs) + 1):
        if keys[i:i + len(inputs)] == inputs:
            return [*runs[:i], out, *runs[i + len(inputs):]]
    raise Superseded(f"{out['key']}: inputs {inputs} are not consecutive runs of the newest manifest ({keys})")


# ── Storage ────────────────────────────────────────────────────────────────


class RunStore(Protocol):
    """A generation's keys (relative to `static-names/<gen>/`) in the data bucket, and the merge lease (scratch)."""

    gen: str

    def listing(self, prefix: str) -> dict[str, int]:
        """`{key: size}` under `prefix`."""
    def keys(self, prefix: str) -> list[str]: ...
    def exists(self, key: str) -> bool: ...
    def read_json(self, key: str) -> dict: ...
    def create(self, key: str, text: str) -> None:
        """Write a new key; `FileExistsError` if it exists (never overwrites)."""
    def upload(self, local: Path, prefix: str) -> list[dict]:
        """Every file under `local` → `prefix/<rel>`, the top `meta.json` last; `{key, size}` per file."""
    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        """Take the merge lease for `owner`: None when taken, else the holder's record."""
    def release(self, owner: str) -> None: ...


class LocalRunStore:
    """`RunStore` over local dirs: the generation's (`root`) and the scratch side's (tests, and a local dry run)."""

    def __init__(self, root: Path, scratch: Path, gen: str = "local"):
        self.root, self.scratch, self.gen = Path(root), Path(scratch), gen

    def listing(self, prefix: str) -> dict[str, int]:
        return {k: p.stat().st_size for p in sorted(self.root.rglob("*")) if p.is_file() and (k := p.relative_to(self.root).as_posix()).startswith(prefix)}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def read_json(self, key: str) -> dict:
        return json.loads((self.root / key).read_text())

    def create(self, key: str, text: str) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)

    def upload(self, local: Path, prefix: str) -> list[dict]:
        files = sorted((p for p in local.rglob("*") if p.is_file()), key=lambda p: (p == local / "meta.json", p))
        out = []
        for p in files:
            key = f"{prefix}/{p.relative_to(local).as_posix()}"
            (self.root / key).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, self.root / key)
            out.append({"key": key, "size": p.stat().st_size})
        return out

    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        p = self.scratch / "merge.lease.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                with open(p, "x") as fh:
                    fh.write(json.dumps({"owner": owner, "at": now.isoformat()}) + "\n")
                return None
            except FileExistsError:
                held = json.loads(p.read_text())
                if not lease_stale(held, now, ttl_s):
                    return held
                p.unlink()
        return json.loads(p.read_text())

    def release(self, owner: str) -> None:
        p = self.scratch / "merge.lease.json"
        if p.exists() and json.loads(p.read_text())["owner"] == owner:
            p.unlink()


class GcsRunStore:
    """`RunStore` over the data bucket (`gs://<bucket>/static-names/<gen>/`) and the scratch bucket's lease."""

    def __init__(self, bucket: str, scratch: str | None, gen: str, client=None):
        """`scratch`: the lease's bucket (None: a store that never takes it, e.g. `publish`'s)."""
        from google.cloud import storage

        self.client = client or storage.Client()
        self.bucket, self.scratch, self.gen, self.prefix = bucket, scratch, gen, f"{PREFIX}/{gen}"
        self.b = self.client.bucket(bucket)
        self.sb = self.client.bucket(scratch) if scratch else None

    def listing(self, prefix: str) -> dict[str, int]:
        return {x.name.removeprefix(self.prefix + "/"): int(x.size or 0) for x in self.client.list_blobs(self.bucket, prefix=f"{self.prefix}/{prefix}")}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def exists(self, key: str) -> bool:
        return self.b.blob(f"{self.prefix}/{key}").exists()

    def read_json(self, key: str) -> dict:
        return json.loads(self.b.blob(f"{self.prefix}/{key}").download_as_bytes())

    def create(self, key: str, text: str) -> None:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self.b.blob(f"{self.prefix}/{key}").upload_from_string(text, if_generation_match=0)
        except PreconditionFailed as e:
            raise FileExistsError(key) from e

    def upload(self, local: Path, prefix: str) -> list[dict]:
        from .static_names import upload_tree

        return upload_tree(local, self.bucket, f"{self.prefix}/{prefix}", last=("meta.json",))

    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.sb.blob(f"{self.prefix}/merge.lease.json")
        for _ in range(2):
            try:
                blob.upload_from_string(json.dumps({"owner": owner, "at": now.isoformat()}) + "\n", if_generation_match=0)
                return None
            except PreconditionFailed:
                try:
                    blob.reload()
                    held = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
                    if not lease_stale(held, now, ttl_s):
                        return held
                    blob.delete(if_generation_match=blob.generation)
                except (NotFound, PreconditionFailed):
                    continue
        return {"owner": "?", "at": "?"}

    def release(self, owner: str) -> None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.sb.blob(f"{self.prefix}/merge.lease.json")
        try:
            blob.reload()
            if json.loads(blob.download_as_bytes(if_generation_match=blob.generation))["owner"] == owner:
                blob.delete(if_generation_match=blob.generation)
        except (NotFound, PreconditionFailed):
            pass


def lease_stale(held: dict, now: datetime, ttl_s: int) -> bool:
    return (now - datetime.fromisoformat(held["at"])).total_seconds() > ttl_s


# ── Building a merged run ──────────────────────────────────────────────────


def tier_resources(tiers: list[str], ram: int, cpus: int, concurrent: bool) -> dict[str, dict]:
    """Each DuckDB tier merge's `mem` (GB, whole) and `threads`: concurrently, `MEM_FRACTION` of `ram` less a
    `STREAM_RESERVE` per streaming tier, split evenly, and the CPUs likewise; one at a time, each gets all of it."""
    duck = [t for t in tiers if t in DUCKDB_TIERS]
    if not duck:
        return {}
    budget = int(ram * MEM_FRACTION)
    if concurrent:
        budget = (budget - STREAM_RESERVE * (len(tiers) - len(duck))) // len(duck)
        cpus = max(1, cpus // len(duck))
    return {t: {"mem": f"{max(1, budget >> 30)}GB", "threads": cpus} for t in duck}


def _machine() -> tuple[int, int]:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), os.cpu_count() or 1


def merge_tier(name: str, dirs: list[Path], outp: Path, run: dict, rule, tmp: Path, mem: str | None = None,
               threads: int | None = None) -> tuple[str, float, dict]:
    """One tier of the merged run `run` from its input run dirs `dirs` (oldest first) into `outp`: `shards` (`sx/`,
    `sidecar*`, `shards.json`), `catalog`, `drill`, or `anchors` (`names/` + `anchors/`). Top-level, so a spawned process
    can run it. Returns `(name, seconds, doc)`."""
    from . import static_append as sa

    t0 = monotonic()
    if name == "shards":
        doc = sa.merge_shards(dirs, outp)
    elif name == "catalog":
        membership = json.loads((dirs[-1] / "catalog" / "meta.json").read_text())["membership"]
        doc = sa.merge_catalogs([d / "catalog" for d in dirs], outp / "catalog", membership, rule)
    elif name == "drill":
        doc = sa.merge_drills(dirs, outp / "drill", run, tmp, threads=threads or 16, mem=mem or "100GB")
    elif name == "anchors":
        from .hex_runs import rule_from_json
        from .static_anchors import Tier as ATier
        from .static_anchors import merge_run_local

        meta = json.loads((dirs[-1] / "anchors" / "meta.json").read_text())
        doc = merge_run_local(connect(threads or 16, mem or "100GB", tmp), [ATier(d) for d in dirs], ATier(outp), meta["R"], meta["K"],
                              run["scans"], rule=rule_from_json(meta.get("hex_runs")))
    else:
        raise ValueError(f"no tier {name!r}")
    return name, round(monotonic() - t0, 1), doc


def run_tiers(dirs: list[Path], drilled: bool) -> list[str]:
    """The tiers a merge of `dirs` writes: shards and catalog always; the drill when the inputs carry it (`plan_carries`
    merges drilled runs only with each other); names + anchors when every input carries them (else the merged run has
    none, and the anchored stack is cut there, as before)."""
    return ["shards", "catalog", *(["drill"] if drilled else []),
            *(["anchors"] if all((d / "anchors" / "meta.json").exists() for d in dirs) else [])]


def build_merged_run(dirs: list[Path], run: dict, outp: Path, *, drilled: bool, rule, tmp: Path, jobs: int = 1,
                     machine: tuple[int, int] | None = None, log: Callable[[str], None] = err) -> dict:
    """The merged run `run` (`plan_carries`' output) of input dirs `dirs` into `outp`: its tiers (`run_tiers`), each in its
    own spawned process when `jobs` > 1 (DuckDB limits per `tier_resources`), then `meta.json` (`run` + the shards' doc).
    Returns `{rows, bytes, …, "s": {tier: seconds}}`."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    shutil.rmtree(outp, ignore_errors=True)
    outp.mkdir(parents=True)
    tiers = run_tiers(dirs, drilled)
    ram, cpus = machine or _machine()
    res = tier_resources(tiers, ram, cpus, concurrent=jobs > 1)
    args = [(t, dirs, outp, run, rule, tmp / f"merge-{t}", res.get(t, {}).get("mem"), res.get(t, {}).get("threads")) for t in tiers]
    got: dict[str, tuple[float, dict]] = {}
    if jobs == 1:
        for a in args:
            name, s, doc = merge_tier(*a)
            got[name] = (s, doc)
            log(f"merge {run['key']}: {name} in {s:.0f}s")
    else:
        with ProcessPoolExecutor(min(jobs, len(args)), mp_context=get_context("spawn")) as ex:
            futures = [ex.submit(merge_tier, *a) for a in args]
            errs = []
            for f in futures:
                try:
                    name, s, doc = f.result()
                    got[name] = (s, doc)
                    log(f"merge {run['key']}: {name} in {s:.0f}s")
                except Exception as e:  # noqa: BLE001 — every tier runs to its end; then the first failure is raised
                    errs.append(e)
            if errs:
                raise errs[0]
    doc = got["shards"][1]
    (outp / "meta.json").write_text(json.dumps({**run, **doc}, indent=1) + "\n")
    return {**doc, "s": {k: v[0] for k, v in got.items()}}


# ── The merge loop ─────────────────────────────────────────────────────────


def newest(store: RunStore) -> tuple[str, dict] | None:
    keys = manifest_keys(store.keys("manifests/"))
    return (keys[-1], store.read_json(keys[-1])) if keys else None


def drilled_runs(store: RunStore, runs: list[dict]) -> set[str]:
    return {r["key"] for r in runs if store.exists(f"{r['key']}/drill/meta.json")}


def complete(store: RunStore, run: dict) -> bool:
    """Whether `run`'s dir is whole on the store: its `meta.json` (uploaded last) names the planned scans, and every reader
    file is there (`missing_files`)."""
    from .static_append import missing_files

    if not store.exists(f"{run['key']}/meta.json") or store.read_json(f"{run['key']}/meta.json").get("scans") != run["scans"]:
        return False
    return not missing_files([run], store.exists, lambda k: store.read_json(f"{k}/drill/meta.json") if store.exists(f"{k}/drill/meta.json") else None)


@dataclass
class Published:
    key: str
    runs: list[dict]


def publish_revision(store: RunStore, inputs: list[str], out: dict, *, attempts: int = 5, log: Callable[[str], None] = err) -> Published | None:
    """Publish the merged run `out` (in place of the runs keyed `inputs`) as a revision of the newest manifest, rebased
    onto whichever manifest is newest when it's written, and re-checked after: until the newest manifest lists `out`.
    None when the newest already did. Refuses (`RuntimeError`) a revision whose runs lack a reader file, or whose drill
    covers fewer scans than the manifest it revises (`static_append`'s rules 1 and 3)."""
    from .static_append import drill_scans, missing_files

    for _ in range(attempts):
        key, m = newest(store)
        runs = rebase(m["runs"], inputs, out)
        if runs is None:
            return None
        scan, _ = parse_manifest(key.removeprefix("manifests/"))
        revs = [p[1] for k in store.keys(f"manifests/{scan}") if (p := parse_manifest(k.removeprefix("manifests/"))) and p[0] == scan]
        name = f"manifests/{manifest_name(scan, max(revs) + 1)}"

        def drill_meta(k: str) -> dict | None:
            return store.read_json(f"{k}/drill/meta.json") if store.exists(f"{k}/drill/meta.json") else None

        if missing := missing_files(runs, store.exists, drill_meta):
            raise RuntimeError(f"not publishing {name}: listed runs lack {missing}")
        if lost := sorted(set(drill_scans(m["runs"], drilled_runs(store, m["runs"]))) - set(drill_scans(runs, drilled_runs(store, runs)))):
            raise RuntimeError(f"not publishing {name}: its runs' drill would no longer cover {lost}")
        doc = {**m, "runs": [{k: r[k] for k in ("key", "first", "last", "level", "scans", "rows", "bytes") if k in r} for r in runs],
               "rev": max(revs) + 1, "revises": key}
        if [s for r in doc["runs"] for s in r["scans"]] != [s for r in m["runs"] for s in r["scans"]]:
            raise RuntimeError(f"not publishing {name}: its runs' scans differ from {key}'s")
        try:
            store.create(name, json.dumps(doc, indent=1) + "\n")
        except FileExistsError:
            log(f"merge {out['key']}: {name} appeared meanwhile; rebasing")
            continue
        if newest(store)[0] == name:
            return Published(name, runs)
        log(f"merge {out['key']}: a newer manifest than {name} landed meanwhile; rebasing onto it")
    raise RuntimeError(f"merge {out['key']}: no revision stayed newest in {attempts} attempts")


def merge_pending(store: RunStore, mount: Path, *, tmp: Path, rule=None, jobs: int = 1, owner: str | None = None, dry_run: bool = False,
                  max_merges: int | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  build: Callable[..., dict] = build_merged_run, log: Callable[[str], None] = err) -> dict:
    """Run the newest manifest's due carries (`plan_carries`), one merged run at a time, each published as a revision
    (`publish_revision`) before the next is planned, until none is due (or `max_merges`). `mount`: the generation's dir
    on a mount of the data bucket (the inputs are read from it). Holds the merge lease throughout (returns
    `{"held": …}` without merging when another holder has it). A failure leaves the newest manifest as it was (every run
    it lists whole), and a rerun resumes: a merged dir already whole is reused."""
    owner = owner or f"{os.environ.get('BATCH_JOB_UID') or socket.gethostname()}:{os.getpid()}"
    cur = newest(store)
    if cur is None:
        return {"merged": [], "manifest": None}
    if dry_run:
        _, merges = plan_carries(cur[1]["runs"], drilled_runs(store, cur[1]["runs"]))
        return {"manifest": cur[0], "plan": [{"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"]} for ins, out in merges]}
    if held := store.lease(owner, now(), LEASE_S):
        log(f"merge: the lease is held ({held}); not merging")
        return {"held": held, "merged": [], "manifest": cur[0]}
    done: list[dict] = []
    try:
        while max_merges is None or len(done) < max_merges:
            _, m = newest(store)
            drilled = drilled_runs(store, m["runs"])
            _, merges = plan_carries(m["runs"], drilled)
            if not merges:
                break
            ins, out = merges[0]
            t0 = monotonic()
            if complete(store, out):
                log(f"merge {out['key']}: already whole; publishing it")
                meta = store.read_json(f"{out['key']}/meta.json")
                doc = {"rows": meta.get("rows"), "bytes": meta.get("bytes"), "s": {}}
            else:
                outp = tmp / "merge" / out["key"]
                doc = build([mount / r["key"] for r in ins], out, outp, drilled=all(r["key"] in drilled for r in ins), rule=rule, tmp=tmp,
                            jobs=jobs, log=log)
                # a merged run is written once: an earlier attempt may have left only keys this one writes (overwritten here)
                ours = {f"{out['key']}/{f.relative_to(outp).as_posix()}" for f in outp.rglob("*") if f.is_file()}
                if stale := sorted(set(store.keys(f"{out['key']}/")) - ours):
                    raise RuntimeError(f"{out['key']}/ holds {len(stale)} objects this merge doesn't write (e.g. {stale[0]}): not merging into it")
                up = store.upload(outp, out["key"])
                log(f"merge {out['key']}: uploaded {len(up)} files, {sum(f['size'] for f in up) / 2**30:.2f} GiB")
                shutil.rmtree(outp)
            out = {**out, **{k: doc[k] for k in ("rows", "bytes") if doc.get(k) is not None}}
            pub = publish_revision(store, [r["key"] for r in ins], out, log=log)
            done.append({"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"], "scans": len(out["scans"]),
                         "rows": out.get("rows"), "manifest": pub.key if pub else None, "tiers_s": doc["s"], "s": round(monotonic() - t0, 1)})
            log(f"merge {out['key']} ({len(ins)} runs, {len(out['scans'])} scans): {pub.key if pub else 'already listed'} in {done[-1]['s']:.0f}s")
    finally:
        store.release(owner)
    return {"merged": done, "manifest": newest(store)[0]}


# ── CLI (the Batch task) ───────────────────────────────────────────────────


@group("merge")
def cli() -> None:
    """Deferred carries of the static name index's runs (the Batch side of `runs merge`)."""


@cli.command("carry")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-g", "--gen", required=True, help="Base generation")
@option("-j", "--jobs", default=4, type=IntRange(min=1), help="Tier merges at once (each in its own process)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the merges read the runs)")
@option("-N", "--max-merges", type=IntRange(min=1), help="Stop after this many merged runs")
@option("-n", "--dry-run", is_flag=True, help="Print the plan; merge and write nothing")
@option("-S", "--scratch", default=scratch_bucket, help="Scratch bucket (the merge lease)")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir for merges")
def carry_cmd(bucket, gen, jobs, mount, max_merges, dry_run, scratch, tmp) -> None:
    """Run the newest manifest's due carries (`plan_carries`): each merged run into its own dir, then a revision
    `manifests/<id>.m<NNN>.json` listing it. One merger per generation (a lease in the scratch bucket); resumable."""
    from .static_names import gen_rule_at

    store = GcsRunStore(bucket, scratch, gen)
    doc = merge_pending(store, Path(mount) / PREFIX / gen, tmp=Path(tmp), rule=gen_rule_at(bucket, gen), jobs=jobs, dry_run=dry_run,
                        max_merges=max_merges)
    print(json.dumps(doc, indent=1))


if __name__ == "__main__":
    cli()
