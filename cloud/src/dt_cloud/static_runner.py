"""`dt-cloud static-names runs add SCAN_ID`: append one scan to the static name index as a run (specs/static-append.md),
in Python, so a deployment's scan job calls it directly (no `gcloud`: GCS and Batch over their APIs):

    prepare → append (key ranges on Batch) → shards ∥ catalog → [drill (long ∥ short)] ∥ [anchors] → publish (local:
      `manifests/<id>.json`, the run at level 0) → R2 (each run the manifest lists, its `drill/meta.json` last; then a check
      that every served file of those runs is on R2; then the manifest, last) [→ verify] → prune
    then, once after the scans: merge (the newest manifest's due carries, `static_merge`, one Batch job waited on up to
      `-w` s; then R2 for the revision it publishes) — non-fatal: the store is servable whatever becomes of it

The drill stage (the heavy-term drilldown, `static_drill`) runs when the profile says so (`drill`): one Batch job of two
tasks, `build -k task` (task 0 the long kind, task 1 the short; the second to finish writes `meta.json`). The anchors
stage (`static_anchors run`, the profile's `anchors`) reads none of the drill's output (nor the drill its), so the two
run at once; each keeps its own skip-if-done and post-check, and a failure of either fails the run once both end.

The unit is a scan: any deployment may scan once a day, every 6 h, or once more on demand, and each scan is its own
run (specs/scan-ids-not-dates.md). Scans are added **strictly in scan-id order**: given SCAN_ID, every published scan
after the generation's newest (base + live runs) and up to SCAN_ID is pending; the oldest pending must be SCAN_ID
itself, else it refuses (exit 3), or with `-c` catches up every pending scan in order. Each stage is skipped when its
output is in the data bucket, so a rerun resumes at the first missing one; the R2 copy skips objects already there,
so it always runs.

Exit status: 0 done, 3 not published yet or not the next scan, 1 a stage failed (the merge stage never fails the run).

Config: the deployment profile (`static_profile`: `STATIC_NAMES_PROFILE` = an example or a JSON file, then each
field's env var); a run needs its generation, buckets, layouts, region, image, accounts, R2 bucket and R2 secrets.
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from math import ceil
from time import monotonic
from typing import Callable

from click import argument, command, option

from .cost_labels import label_batch_spec
from .scan_id import SCAN_ID
from .static_merge import parse_manifest, plan_carries
from .static_names import PREFIX, err
from .static_profile import Profile, profile

#: Exit status: the scan is not published yet, or is not the next one to append.
NOT_NEXT = 3


class NotNext(Exception):
    """The scan can't be appended now (not published, or an earlier published scan is pending): exit 3."""


class StillRunning(Exception):
    """A Batch job outlived the wait given for it (it keeps running; nothing is cancelled)."""


def ready(p: Profile, gen: str | None = None) -> Profile:
    """`p` (with `gen` over its own) checked for everything a run needs; the project defaults to the credentials'."""
    if gen:
        p = replace(p, gen=gen)
    if not p.project:
        from .gcp import gcp_project

        p = replace(p, project=gcp_project())
    for f in ("gen", "bucket", "scratch", "layouts", "region", "image", "sa", "r2_bucket"):
        p.need(f)
    p.hex_rule()
    p.r2_env_secrets()
    return p


# ── Order ──────────────────────────────────────────────────────────────────


def pending_scans(have: list[str], published: list[str], scan_id: str, catch_up: bool) -> list[str]:
    """The scans to append for a run given `scan_id`, oldest first: the published scans after `have[-1]` (the
    generation's newest: base + live runs) through `scan_id`. Raises `NotNext` when `scan_id` is not published, or
    when an earlier published scan is pending and not `catch_up`. `[]` when `scan_id` is already appended."""
    if not SCAN_ID.fullmatch(scan_id):
        raise ValueError(f"{scan_id!r} is not a scan id")
    if scan_id in have:
        return []
    if scan_id <= have[-1]:
        raise NotNext(f"{scan_id} precedes the generation's newest scan {have[-1]} but was never appended: it can't join now")
    if scan_id not in published:
        raise NotNext(f"{scan_id} is not published (no path sort under the generation's layouts)")
    todo = sorted(s for s in published if have[-1] < s <= scan_id)
    if todo[0] != scan_id and not catch_up:
        raise NotNext(f"{len(todo) - 1} earlier published scan(s) pending: {', '.join(todo[:-1])} (append them first, or -c)")
    return todo


# ── Batch ──────────────────────────────────────────────────────────────────


def task_command(cfg: Profile, module: str, args: list[str], *, mount: bool = True) -> str:
    """A stage task's shell command: `python -m dt_cloud.<module> <args> [-m /gcs/<bucket>]`, from the image's code or
    (`cfg.src`) the given mounted dirs."""
    prep = "mkdir -p /stage/tmp /stage/out"
    py = "python3 -u -m"
    if cfg.src:
        prep += " /stage/src && cp -r " + " ".join(shlex.quote(s) for s in cfg.src) + " /stage/src/"
        py = "PYTHONPATH=/stage/src " + py
    m = ["-m", f"/gcs/{cfg.bucket}"] if mount else []
    return f"set -euo pipefail; {prep} && cd /stage && {py} dt_cloud.{module} {shlex.join([*args, *m])}"


def job_spec(cfg: Profile, name: str, tasks: int, commands: list[str], *, stage: str, scratch: bool = True, r2: bool = False,
             machine: str | None = None, ssd_gb: int | None = None, purpose: str = "static-names", component: str | None = None) -> dict:
    """A Batch job of `tasks` tasks (in parallel) running `commands` (one shell script, `&&`-chained) in the image, the
    data bucket (and `scratch`) mounted read-only under /gcs, a local SSD at /stage. `r2`: as the R2 account, with its
    credentials from Secret Manager. `purpose` labels the job; `component` is its cost label (default: the stage's,
    `STAGE_COMPONENTS`)."""
    machine = machine or cfg.machine
    vcpus = int(machine.rsplit("-", 1)[-1])
    buckets = [cfg.bucket, *([cfg.scratch] if scratch else [])]
    env = {"STATIC_NAMES_BUCKET": cfg.bucket, "STATIC_NAMES_SCRATCH": cfg.scratch}
    environment: dict = {"variables": env}
    if r2:
        env["R2_BUCKET"] = cfg.r2_bucket
        if "endpoint" not in cfg.r2_secrets:
            env["R2_ENDPOINT"] = cfg.r2_endpoint
        secrets = cfg.r2_env_secrets()
        environment["secretVariables"] = {k: f"projects/{cfg.project}/secrets/{v}/versions/latest" for k, v in sorted(secrets.items())}
    return label_batch_spec({
        "taskGroups": [{
            "taskCount": tasks,
            "parallelism": tasks,
            "taskSpec": {
                "runnables": [{"container": {
                    "imageUri": cfg.need("image"),
                    "entrypoint": "bash",
                    "commands": ["-c", " && ".join(f"( {c} )" for c in commands) if len(commands) > 1 else commands[0]],
                    "volumes": [*(f"/mnt/disks/gcs/{b}:/gcs/{b}:ro" for b in buckets), "/mnt/disks/stage:/stage:rw"],
                }}],
                "environment": environment,
                "computeResource": {"cpuMilli": vcpus * 1000, "memoryMib": vcpus * 7700},
                "maxRetryCount": 3 if cfg.spot else 0,
                "maxRunDuration": "14400s",
                "volumes": [
                    *({"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]} for b in buckets),
                    {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
                ],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {
                "machineType": machine,
                "provisioningModel": "SPOT" if cfg.spot else "STANDARD",
                "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(ssd_gb or cfg.ssd_gb)}, "deviceName": "stage"}],
            }}],
            "serviceAccount": {"email": (cfg.r2_sa or cfg.sa) if r2 else cfg.sa},
            "location": {"allowedLocations": [f"regions/{cfg.region}"]},
        },
        "labels": {"purpose": purpose, "stage": stage, "gen": _label(cfg.gen)},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }, component or STAGE_COMPONENTS.get(stage, "static-names"))


#: A stage's cost-attribution `component` (`cost_labels`); the rest of the chain is `static-names`.
STAGE_COMPONENTS = {"drill": "drill", "anchors": "anchors"}


def _label(v: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in v.lower())[:63]


def job_id(stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`sn-<stage>-<scan>-<hhmmss>`: Batch ids are lowercase letters, digits and hyphens."""
    t = (now or datetime.now(timezone.utc)).strftime("%H%M%S")
    return f"sn-{stage}-{scan_id.lower().replace('t', '-')}-{t}"


class BatchRunner:
    """Submit a Batch job and wait for it (REST over ADC, `batch.submit_job` / `gcp.batch_job`)."""

    def __init__(self, cfg: Profile, log: Callable[[str], None], *, delay: int = 20, max_delay: int = 30):
        """Polls every `delay` s, doubling up to `max_delay`: each stage's end is noticed up to one poll late, so the cap
        stays low (at 120 s, 10-10's chain lost minutes between stages)."""
        self.cfg, self.log, self.delay, self.max_delay = cfg, log, delay, max_delay

    def __call__(self, name: str, spec: dict, wait: float | None = None) -> None:
        """Submit, then poll until the job ends; `wait`: stop polling after that many seconds (`StillRunning`; the job runs on)."""
        from .batch import submit_job
        from .gcp import batch_job

        submit_job(spec, name, region=self.cfg.region)
        self.log(f"submitted {name}")
        delay, t0 = self.delay, monotonic()
        while True:
            if wait is not None and monotonic() - t0 >= wait:
                raise StillRunning(f"Batch job {name}: still running after {wait:.0f}s")
            st = batch_job(name, project=self.cfg.project, region=self.cfg.region).get("status", {})
            state = st.get("state", "?")
            counts = " ".join(f"{k}={v}" for g in (st.get("taskGroups") or {}).values() for k, v in sorted(g.get("counts", {}).items()))
            self.log(f"{name} {state} {counts}")
            if state == "SUCCEEDED":
                return
            if state in ("FAILED", "DELETION_IN_PROGRESS", "CANCELLED"):
                raise RuntimeError(f"Batch job {name}: {state}")
            time.sleep(delay if wait is None else max(0.0, min(delay, wait - (monotonic() - t0))))
            delay = min(delay * 2, self.max_delay)


# ── The chain ──────────────────────────────────────────────────────────────


@dataclass
class Runner:
    """The chain over injectable effects (tests pass fakes): `exists(key)` / `count(prefix, suffix)` / `read_json(key)` on
    the data bucket (keys relative to it), `published()` the scan ids under the base's layouts, `run_job(name, spec[, wait])`,
    `prepare(scan_id)`, `publish(scan_id)` and `prune(scan_id)` (the `runs` CLI's local stages), `list_keys(prefix)`, `log`.
    `merge`: run the merge stage after the scans; `merge_wait`: how long it waits on its Batch job (None: to its end)."""
    cfg: Profile
    exists: Callable[[str], bool]
    count: Callable[[str, str], int]
    read_json: Callable[[str], dict]
    published: Callable[[list[str] | None, str], list[str]]
    run_job: Callable[[str, dict], None]
    prepare: Callable[[str], None]
    prune: Callable[[str], None]
    publish: Callable[[str], None]
    list_keys: Callable[[str], list[str]] = lambda prefix: []
    log: Callable[[str], None] = err
    dry_run: bool = False
    verify_terms: str | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    merge: bool = True
    merge_wait: float | None = 0

    @property
    def root(self) -> str:
        return f"{PREFIX}/{self.cfg.gen}"

    def have(self) -> tuple[list[str], list[str] | None]:
        """The generation's scans (base + the newest manifest's runs), and the base's layouts. Runs follow the base's
        recorded hex-run rule (every stage reads it from the base's `scans.json`), so a run's tiers always agree; a
        profile whose rule differs (a deployment that adopts the rule at its next generation) is logged, not applied."""
        from .hex_runs import rule_from_json

        base = self.read_json(f"{self.root}/scans.json")
        recorded, wanted = rule_from_json(base.get("hex_runs")), self.cfg.hex_rule()
        if recorded != wanted:
            self.log(f"{self.cfg.gen}: built with hex_runs {recorded.to_json() if recorded else 'off'}, the profile says "
                     f"{wanted.to_json() if wanted else 'off'}: its runs keep the generation's rule (the profile's applies to the next generation)")
        keys = self.manifests()
        runs = self.read_json(keys[-1])["runs"] if keys else []
        return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])], base.get("layouts")

    def manifests(self) -> list[str]:
        """The manifests' keys, oldest first: by scan, then revision (`static_merge.manifest_keys`)."""
        from .static_merge import manifest_keys

        return [f"{self.root}/{k}" for k in manifest_keys([k.removeprefix(f"{self.root}/") for k in self.list_keys(f"{self.root}/manifests/")])]

    def run(self, scan_id: str, catch_up: bool = False) -> list[str]:
        """Append `scan_id` (and with `catch_up` every earlier pending scan, in order). Returns the scans appended."""
        have, layouts = self.have()
        todo = pending_scans(have, self.published(layouts, have[-1]), scan_id, catch_up)
        if not todo:
            self.log(f"{scan_id}: already appended; {'its drill, ' if self.cfg.drill else ''}the R2 copy and prune only")
            if scan_id == have[-1]:
                runs = self.read_json(self.manifests()[-1])["runs"] if self.manifests() else []
                if f"deltas/{scan_id}" in {r["key"] for r in runs}:
                    self.drill_anchors(scan_id)
                self.r2(scan_id)
                self.stage(f"{scan_id} prune", lambda: self.prune(scan_id))
            self.carries()
            return []
        if len(todo) > 1:
            self.log(f"catching up {len(todo)} scans: {', '.join(todo)}")
        for s in todo:
            self.one(s)
        self.carries()
        return todo

    def stage(self, name: str, fn: Callable[[], None]) -> None:
        if self.dry_run:
            self.log(f"{name}: would run")
            return
        t = monotonic()
        self.log(f"{name}: start")
        fn()
        self.log(f"{name}: done in {monotonic() - t:.0f}s")

    def concurrently(self, d: str, jobs: list[tuple[str, Callable[[], None]]]) -> None:
        """`jobs` (`(label, fn)`, each a stage's job and its post-check) as one stage, at once (a lone one as itself). Each
        runs to its end (a failure doesn't stop the other, whose output is kept, so a rerun resumes only the failed one);
        then the failure is raised, or with several a `RuntimeError` naming each."""
        if not jobs:
            return
        if len(jobs) == 1:
            (label, fn), = jobs
            self.stage(f"{d} {label}", fn)
            return

        def timed(label: str, fn: Callable[[], None]) -> None:
            t = monotonic()
            try:
                fn()
            except Exception as e:
                self.log(f"{d} {label}: failed after {monotonic() - t:.0f}s: {e}")
                raise
            self.log(f"{d} {label}: done in {monotonic() - t:.0f}s")

        def all_() -> None:
            with ThreadPoolExecutor(len(jobs)) as ex:
                futures = [(label, ex.submit(timed, label, fn)) for label, fn in jobs]
            errs = [(label, e) for label, f in futures if (e := f.exception()) is not None]
            if len(errs) == 1:
                raise errs[0][1]
            if errs:
                raise RuntimeError("; ".join(f"{label}: {e}" for label, e in errs)) from errs[0][1]
        self.stage(f"{d} {' ∥ '.join(label for label, _ in jobs)}", all_)

    def job(self, stage: str, scan_id: str, tasks: int, module: str, args: list[str], **kw) -> tuple[str, dict]:
        name = job_id(stage, scan_id, self.now())
        cmd = task_command(self.cfg, module, args, mount=kw.pop("mount", True))
        return name, job_spec(self.cfg, name, tasks, [cmd], stage=stage, **kw)

    def one(self, d: str) -> None:
        run = f"{self.root}/deltas/{d}"
        g = ["-g", self.cfg.gen, "-d", d]
        self.log(f"{d}: gen {self.cfg.gen}")
        # 1. prepare (local): pins the scan as the run's `scans.json` (checks it is the next one).
        if self.exists(f"{run}/scans.json"):
            self.log(f"{d} prepare: done")
        else:
            self.stage(f"{d} prepare", lambda: self.prepare(d))
        # 2. append: every key range, `append_tasks` tasks.
        k = self.read_json(f"{self.root}/ranges.json")["k"]
        n = self.count(f"{run}/dhist/", ".parquet")
        if n >= k:
            self.log(f"{d} append: done ({n}/{k} ranges)")
        else:
            per = ceil(k / self.cfg.append_tasks)
            name, spec = self.job("append", d, ceil(k / per), "static_append", ["append", *g, "-n", str(per)])
            self.stage(f"{d} append ({n}/{k} ranges)", lambda: self.run_job(name, spec))
        # 3. shards ∥ catalog (both read only the run's `cdelta/`).
        jobs = []
        for stage, done in (("shards", "sidecar.parquet"), ("catalog", "catalog/meta.json")):
            if self.exists(f"{run}/{done}"):
                self.log(f"{d} {stage}: done")
            else:
                jobs.append((stage, partial(self.run_job, *self.job(stage, d, 1, "static_append", [stage, *g]))))
        self.concurrently(d, jobs)
        # 4. drill ∥ anchors (the profile's `drill`, `anchors`): neither reads the other's output, so they run at once.
        self.drill_anchors(d)
        # 5. publish (local, seconds): `manifests/<d>.json` (written once), the run at level 0; carries come after (`carries`).
        if self.exists(f"{self.root}/manifests/{d}.json"):
            self.log(f"{d} publish: done")
        else:
            self.stage(f"{d} publish", lambda: self.publish(d))
        # 6. R2: the runs the manifest lists, then the manifest, last.
        self.r2(d)
        # 7. verify (optional): brute force from the scan file vs the base + runs.
        if self.verify_terms:
            if self.exists(f"{run}/verify.json"):
                self.log(f"{d} verify: done")
            else:
                name, spec = self.job("verify", d, 1, "static_append", ["verify", *g, "-t", self.verify_terms])
                self.stage(f"{d} verify", lambda: self.run_job(name, spec))
        # 8. prune: only the newest complete open-version state is kept.
        self.stage(f"{d} prune", lambda: self.prune(d))

    def drill_anchors(self, d: str) -> None:
        """The run's drill and anchors (each when the profile says so and its output is missing), at once: the drill reads
        the run's `sx/`, `cdelta/`, `sidecar.parquet` and catalog and the earlier runs' `drill/`; the anchors the run's
        `sx/` and `cdelta/` and the earlier tiers' `names/` and `anchors/`; neither the other's (publish, after both,
        merges each)."""
        self.concurrently(d, [j for j in (self.cfg.drill and self.drill(d), self.cfg.anchors and self.anchors(d)) if j])

    def drill(self, d: str) -> tuple[str, Callable[[], None]] | None:
        """The run's `drill/` job (None when it's done): one job of two tasks (long ∥ short), each on a whole machine;
        done once `meta.json` is there."""
        run = f"{self.root}/deltas/{d}"
        if self.exists(f"{run}/drill/meta.json"):
            self.log(f"{d} drill: done")
            return None
        vcpus = int(self.cfg.machine.rsplit("-", 1)[-1])
        args = ["build", "-g", self.cfg.gen, "-d", d, "-k", "task", "-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus)]
        name, spec = self.job("drill", d, 2, "static_drill", args)

        def build():
            self.run_job(name, spec)
            if not self.exists(f"{run}/drill/meta.json"):
                raise RuntimeError(f"{run}/drill/meta.json: not written (both tasks succeeded)")
        return "drill (long ∥ short)", build

    def anchors(self, d: str) -> tuple[str, Callable[[], None]] | None:
        """The run's `names/` and `anchors/` job (`static_anchors run`; None when it's done): one task; done once
        `anchors/meta.json` is there, and `anchors/start/meta.json` (the starts-with catalog) when the base carries one."""
        run = f"{self.root}/deltas/{d}"
        start = self.exists(f"{self.root}/anchors/start/meta.json")
        if self.exists(f"{run}/anchors/meta.json") and (not start or self.exists(f"{run}/anchors/start/meta.json")):
            self.log(f"{d} anchors: done")
            return None
        vcpus = int(self.cfg.machine.rsplit("-", 1)[-1])
        args = ["run", "-g", self.cfg.gen, "-d", d, "-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus)]
        name, spec = self.job("anchors", d, 1, "static_anchors", args)

        def build():
            self.run_job(name, spec)
            for m in ("anchors/meta.json", *(("anchors/start/meta.json",) if start else ())):
                if not self.exists(f"{run}/{m}"):
                    raise RuntimeError(f"{run}/{m}: not written (the task succeeded)")
        return "anchors", build

    def r2(self, d: str, manifest: str | None = None) -> None:
        """One job: `r2-copy` of each run the manifest `manifests/<manifest>.json` (default `d`'s own) lists (its liveness
        markers last: `drill/meta.json`, then the starts-with catalog's `anchors/start/meta.json` before `anchors/meta.json`,
        so a run's anchors go live with their catalog), then `r2-verify` (every served file of those runs on R2), then
        `r2-copy` of that manifest alone (each copy skips what R2 holds): no manifest reaches R2 before its runs are checked
        there."""
        stem = manifest or d
        key = f"{self.root}/manifests/{stem}.json"
        runs = [r["key"] for r in self.read_json(key)["runs"]] if self.exists(key) else [f"deltas/{d}"]
        if self.dry_run:
            self.log(f"{d} r2: would copy {', '.join(runs)}, check them, then copy manifests/{stem}.json")
            return
        cmds = []
        for r in runs:
            cmds.append(task_command(self.cfg, "static_names", ["r2-copy", "-g", f"{self.cfg.gen}/{r}", "-x", "drill/meta.json", "-x", "anchors/meta.json",
                                                                "-x", "anchors/start/meta.json"], mount=False))
            for m in ("drill/meta.json", "anchors/start/meta.json", "anchors/meta.json"):
                cmds.append(task_command(self.cfg, "static_names", ["r2-copy", "-g", f"{self.cfg.gen}/{r}", "-o", m], mount=False))
        cmds.append(task_command(self.cfg, "static_names", ["r2-verify", "-g", self.cfg.gen, "-m", stem], mount=False))
        cmds.append(task_command(self.cfg, "static_names", ["r2-copy", "-g", self.cfg.gen, "-o", f"manifests/{stem}.json"], mount=False))
        name = job_id("r2", d, self.now())
        spec = job_spec(self.cfg, name, 1, cmds, stage="r2", scratch=False, r2=True, machine="n2-highmem-4", ssd_gb=375)
        self.stage(f"{d} r2 ({len(runs)} runs + manifests/{stem}.json)", lambda: self.run_job(name, spec))

    def carries(self, fatal: bool = False) -> None:
        """The merge stage, once after the scans: the newest manifest's due carries (`static_merge.plan_carries`), when any,
        as one Batch job (`static_merge carry`: each merged run, then a revision of the newest manifest), waited on up to
        `merge_wait` s; then, when the newest manifest is a revision, the R2 job for it (a merge that outlived the wait
        reaches R2 here on a later run, or with the next scan's manifest, which lists its run). Non-fatal: a failure or
        timeout is logged and the store stays as it was, servable (the next run plans again; a merge resumes). `fatal`: raise
        a failure (`RuntimeError`) instead."""
        if not self.merge:
            return
        try:
            keys = self.manifests()
            if not keys:
                return
            m = self.read_json(keys[-1])
            drilled = {r["key"] for r in m["runs"] if self.exists(f"{self.root}/{r['key']}/drill/meta.json")}
            _, merges = plan_carries(m["runs"], drilled)
            if merges:
                desc = "; ".join(f"{len(ins)} runs → {out['key']} (level {out['level']})" for ins, out in merges)
                name, spec = self.job("merge", m["date"], 1, "static_merge", ["carry", "-g", self.cfg.gen])
                try:
                    self.stage(f"merge: {desc}", lambda: self.run_job(name, spec, wait=self.merge_wait))
                except StillRunning as e:
                    self.log(f"merge: {e}; it publishes its revision on GCS when done, and R2 gets it with a later run")
                    return
                if self.dry_run:
                    return
                keys = self.manifests()
            scan, rev = parse_manifest(keys[-1].rsplit("/", 1)[-1])
            if rev:
                self.r2(scan, keys[-1].rsplit("/", 1)[-1].removesuffix(".json"))
        except Exception as e:
            if fatal:
                raise RuntimeError(str(e)) from e
            self.log(f"merge: failed, not fatal (every listed run is whole; the next run plans again): {e}")


def gcs_runner(cfg: Profile, *, dry_run: bool = False, verify_terms: str | None = None, merge: bool = True,
               merge_wait: float | None = 0) -> Runner:
    """`Runner` over the real data bucket, Batch, and the `runs` CLI's `prepare` / `publish` / `prune`."""
    from google.cloud import storage

    from . import static_append as sa
    from .static_names import list_scans, read_json

    client = storage.Client(project=cfg.project)
    b = client.bucket(cfg.bucket)

    def published(layouts, start):
        return [s["id"] for s in list_scans(cfg.bucket, layouts=layouts or cfg.layouts, start=start)["scans"]]

    def prepare(d):
        try:
            sa.prepare_cmd.callback(bucket=cfg.bucket, date=d, gen=cfg.gen)
        except SystemExit as e:
            raise NotNext(str(e)) from e

    def publish(d):
        sa.publish_cmd.callback(bucket=cfg.bucket, date=d, gen=cfg.gen, dry_run=False)

    def prune(d):
        k = read_json(f"gs://{cfg.bucket}/{PREFIX}/{cfg.gen}/ranges.json")["k"]
        doc = sa.prune_state(client, cfg.gen, d, k, bucket=cfg.bucket, scratch=cfg.scratch, dry_run=dry_run)
        err(json.dumps(doc))

    return Runner(
        cfg=cfg,
        exists=lambda key: b.blob(key).exists(),
        count=lambda prefix, suffix: sum(1 for x in client.list_blobs(cfg.bucket, prefix=prefix) if x.name.endswith(suffix)),
        read_json=lambda key: read_json(f"gs://{cfg.bucket}/{key}"),
        published=published,
        run_job=BatchRunner(cfg, err),
        prepare=prepare,
        prune=prune,
        publish=publish,
        dry_run=dry_run,
        verify_terms=verify_terms,
        merge=merge,
        merge_wait=merge_wait,
        list_keys=lambda prefix: [x.name for x in client.list_blobs(cfg.bucket, prefix=prefix)],
    )


@command("add")
@option("-c", "--catch-up", is_flag=True, help="Append every earlier published scan still pending first, in scan-id order")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@option("-M", "--no-merge", is_flag=True, help="Skip the merge stage (the due carries wait for a later run, or `runs merge`)")
@option("-n", "--dry-run", is_flag=True, help="Report each stage's state and what would run; submit and write nothing")
@option("-t", "--verify-terms", help="Also run `runs verify` with this terms file (a gs:// URL)")
@option("-w", "--merge-wait", default=0, type=float, help="Seconds to wait on the merge job (default 0: submit it and go; it publishes on GCS when done)")
@argument("scan_id")
def add_cmd(catch_up: bool, gen: str | None, no_merge: bool, dry_run: bool, verify_terms: str | None, merge_wait: float, scan_id: str) -> None:
    """Append SCAN_ID to the static name index: prepare → append → shards ∥ catalog → [drill] ∥ [anchors] → publish → R2 → prune, each stage
    skipped when its output exists; then the merge stage (the due carries, non-fatal). Exit 3 when SCAN_ID is not published
    yet, or an earlier published scan is pending (without -c)."""
    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=dry_run, verify_terms=verify_terms, merge=not no_merge, merge_wait=merge_wait)
    try:
        done = runner.run(scan_id, catch_up=catch_up)
    except NotNext as e:
        err(f"static-names runs add {scan_id}: {e}")
        sys.exit(NOT_NEXT)
    except RuntimeError as e:
        err(f"static-names runs add {scan_id}: {e}")
        sys.exit(1)
    print(json.dumps({"gen": cfg.gen, "scan": scan_id, "appended": done, "dry_run": dry_run}))


@command("merge")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@option("-n", "--dry-run", is_flag=True, help="Report the due carries; submit and write nothing")
@option("-w", "--wait", type=float, help="Seconds to wait on the merge job (default: to its end, Batch's own cap)")
def merge_cmd(gen: str | None, dry_run: bool, wait: float | None) -> None:
    """The newest manifest's due carries, on their own: the merge job (`static_merge carry`), then R2 for the revision it
    publishes (`runs add`'s merge stage, alone: for a schedule of its own, or to catch up). Exit 1 when it fails (the store
    stays as it was)."""
    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=dry_run, merge_wait=wait)
    try:
        runner.carries(fatal=True)
    except RuntimeError as e:
        err(f"static-names runs merge: {e}")
        sys.exit(1)
    print(json.dumps({"gen": cfg.gen, "manifest": (runner.manifests() or [None])[-1], "dry_run": dry_run}))
