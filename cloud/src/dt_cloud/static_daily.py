"""`dt-cloud static-names daily run SCAN_ID`: the static name index's per-scan append chain (specs/static-daily-append.md),
in Python, so a deployment's scan job calls it directly (no `gcloud`: GCS and Batch over their APIs):

    prepare → append (key ranges on Batch) → shards ∥ catalog → publish (tier merges + `manifests/<id>.json`)
      → R2 (each run the manifest lists, then the manifest, last) [→ verify] → prune

Scans are appended **strictly in scan-id order** (specs/scan-ids-not-dates.md: a scan's key is its id, two scans a day
are two runs). Given SCAN_ID, every published scan after the generation's newest (base + live runs) and up to SCAN_ID
is pending; the oldest pending must be SCAN_ID itself, else the run refuses (exit 3), or with `-c` catches up every
pending scan in order. Each stage is skipped when its output is in the data bucket, so a rerun resumes at the first
missing one; the R2 copy skips objects already there, so it always runs.

Exit status: 0 done, 3 not published yet or not the next scan, 1 a stage failed.

Deployment config (env; the CLI's options override): `STATIC_NAMES_GEN` (the base generation), `STATIC_NAMES_BUCKET`
/ `STATIC_NAMES_SCRATCH` (`static_names`), `GCP_PROJECT`, `STATIC_NAMES_REGION` (us-east1), `STATIC_NAMES_IMAGE` (the job
image: `dt_cloud` + `pyrmts`), `STATIC_NAMES_SA` (the stages' account: the data and scratch buckets),
`STATIC_NAMES_R2_SA` (the R2 copy's account; default the stages'), `R2_BUCKET`, the R2 credentials as Secret Manager
secret names `STATIC_NAMES_R2_SECRETS` (`endpoint=NAME,key_id=NAME,secret=NAME`; `endpoint` may instead be the plain
`R2_ENDPOINT`), `STATIC_NAMES_MACHINE` (n2-highmem-16), `STATIC_NAMES_SSD` (750 GB), `STATIC_NAMES_SPOT` (1),
`STATIC_NAMES_APPEND_TASKS` (16), `STATIC_NAMES_SRC` (comma-separated mounted dirs copied onto the tasks' PYTHONPATH in
place of the image's own code, e.g. `/gcs/<bucket>/static-names/src/<tree>/dt_cloud`).
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from time import monotonic
from typing import Callable

from click import argument, command, option

from .static_names import DATA_BUCKET, GCS_LAYOUTS, PREFIX, SCAN_ID, SCRATCH_BUCKET, err

#: Exit status: the scan is not published yet, or is not the next one to append.
NOT_NEXT = 3


class NotNext(Exception):
    """The scan can't be appended now (not published, or an earlier published scan is pending): exit 3."""


@dataclass(frozen=True)
class Config:
    gen: str
    project: str
    image: str
    sa: str
    r2_bucket: str
    bucket: str = DATA_BUCKET
    scratch: str = SCRATCH_BUCKET
    region: str = "us-east1"
    r2_sa: str | None = None
    #: Secret Manager secret names, by the env var each sets in the R2 copy: `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`,
    #: `R2_SECRET_ACCESS_KEY`. `r2_endpoint` (plain) replaces the endpoint's secret.
    r2_secrets: dict[str, str] = field(default_factory=dict)
    r2_endpoint: str | None = None
    machine: str = "n2-highmem-16"
    ssd_gb: int = 750
    spot: bool = True
    append_tasks: int = 16
    src: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, **over) -> "Config":
        e = os.environ.get
        secrets = {}
        names = {"endpoint": "R2_ENDPOINT", "key_id": "R2_ACCESS_KEY_ID", "secret": "R2_SECRET_ACCESS_KEY"}
        for part in filter(None, (e("STATIC_NAMES_R2_SECRETS") or "").split(",")):
            k, _, v = part.partition("=")
            if k.strip() not in names or not v.strip():
                raise SystemExit(f"STATIC_NAMES_R2_SECRETS: {part!r} is not endpoint=|key_id=|secret=<secret name>")
            secrets[names[k.strip()]] = v.strip()

        def need(name: str, what: str) -> str:
            v = (e(name) or "").strip()
            if not v:
                raise SystemExit(f"{name} is unset: export {what}")
            return v

        kw = {k: v for k, v in over.items() if v is not None}
        if "project" not in kw:
            from .gcp import gcp_project

            kw["project"] = gcp_project()
        cfg = dict(
            gen=kw.pop("gen", None) or need("STATIC_NAMES_GEN", "the base generation (static-names/<gen>/)"),
            image=kw.pop("image", None) or need("STATIC_NAMES_IMAGE", "the job image (dt_cloud + pyrmts)"),
            sa=kw.pop("sa", None) or need("STATIC_NAMES_SA", "the account the stages run as (data + scratch buckets)"),
            r2_bucket=kw.pop("r2_bucket", None) or need("R2_BUCKET", "the R2 bucket serving the index"),
            region=e("STATIC_NAMES_REGION") or "us-east1",
            r2_sa=e("STATIC_NAMES_R2_SA") or None,
            r2_secrets=secrets,
            r2_endpoint=e("R2_ENDPOINT") or None,
            machine=e("STATIC_NAMES_MACHINE") or "n2-highmem-16",
            ssd_gb=int(e("STATIC_NAMES_SSD") or 750),
            spot=(e("STATIC_NAMES_SPOT") or "1") != "0",
            append_tasks=int(e("STATIC_NAMES_APPEND_TASKS") or 16),
            src=tuple(s for s in (e("STATIC_NAMES_SRC") or "").split(",") if s),
            bucket=DATA_BUCKET, scratch=SCRATCH_BUCKET,
        )
        cfg.update(kw)
        out = cls(**cfg)
        if "R2_ENDPOINT" not in out.r2_secrets and not out.r2_endpoint:
            raise SystemExit("the R2 endpoint: set R2_ENDPOINT, or name its secret in STATIC_NAMES_R2_SECRETS (endpoint=…)")
        for part, k in (("key_id", "R2_ACCESS_KEY_ID"), ("secret", "R2_SECRET_ACCESS_KEY")):
            if k not in out.r2_secrets:
                raise SystemExit(f"STATIC_NAMES_R2_SECRETS needs {part}=<secret name>")
        return out


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


def task_command(cfg: Config, module: str, args: list[str], *, mount: bool = True) -> str:
    """A stage task's shell command: `python -m dt_cloud.<module> <args> [-m /gcs/<bucket>]`, from the image's code or
    (`cfg.src`) the given mounted dirs."""
    prep = "mkdir -p /stage/tmp /stage/out"
    py = "python3 -u -m"
    if cfg.src:
        prep += " /stage/src && cp -r " + " ".join(shlex.quote(s) for s in cfg.src) + " /stage/src/"
        py = "PYTHONPATH=/stage/src " + py
    m = ["-m", f"/gcs/{cfg.bucket}"] if mount else []
    return f"set -euo pipefail; {prep} && cd /stage && {py} dt_cloud.{module} {shlex.join([*args, *m])}"


def job_spec(cfg: Config, name: str, tasks: int, commands: list[str], *, stage: str, scratch: bool = True, r2: bool = False,
             machine: str | None = None, ssd_gb: int | None = None) -> dict:
    """A Batch job of `tasks` tasks (in parallel) running `commands` (one shell script, `&&`-chained) in the image, the
    data bucket (and `scratch`) mounted read-only under /gcs, a local SSD at /stage. `r2`: as the R2 account, with its
    credentials from Secret Manager."""
    machine = machine or cfg.machine
    vcpus = int(machine.rsplit("-", 1)[-1])
    buckets = [cfg.bucket, *([cfg.scratch] if scratch else [])]
    env = {"STATIC_NAMES_BUCKET": cfg.bucket, "STATIC_NAMES_SCRATCH": cfg.scratch}
    environment: dict = {"variables": env}
    if r2:
        env["R2_BUCKET"] = cfg.r2_bucket
        if cfg.r2_endpoint and "R2_ENDPOINT" not in cfg.r2_secrets:
            env["R2_ENDPOINT"] = cfg.r2_endpoint
        environment["secretVariables"] = {k: f"projects/{cfg.project}/secrets/{v}/versions/latest" for k, v in sorted(cfg.r2_secrets.items())}
    return {
        "taskGroups": [{
            "taskCount": tasks,
            "parallelism": tasks,
            "taskSpec": {
                "runnables": [{"container": {
                    "imageUri": cfg.image,
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
        "labels": {"purpose": "static-names", "stage": stage, "gen": _label(cfg.gen)},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def _label(v: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in v.lower())[:63]


def job_id(stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`sn-<stage>-<scan>-<hhmmss>`: Batch ids are lowercase letters, digits and hyphens."""
    t = (now or datetime.now(timezone.utc)).strftime("%H%M%S")
    return f"sn-{stage}-{scan_id.lower().replace('t', '-')}-{t}"


class BatchRunner:
    """Submit a Batch job and wait for it (REST over ADC, `batch.submit_job` / `gcp.batch_job`)."""

    def __init__(self, cfg: Config, log: Callable[[str], None]):
        self.cfg, self.log = cfg, log

    def __call__(self, name: str, spec: dict) -> None:
        from .batch import submit_job
        from .gcp import batch_job

        submit_job(spec, name, region=self.cfg.region)
        self.log(f"submitted {name}")
        delay = 20
        while True:
            st = batch_job(name, project=self.cfg.project, region=self.cfg.region).get("status", {})
            state = st.get("state", "?")
            counts = " ".join(f"{k}={v}" for g in (st.get("taskGroups") or {}).values() for k, v in sorted(g.get("counts", {}).items()))
            self.log(f"{name} {state} {counts}")
            if state == "SUCCEEDED":
                return
            if state in ("FAILED", "DELETION_IN_PROGRESS", "CANCELLED"):
                raise RuntimeError(f"Batch job {name}: {state}")
            time.sleep(delay)
            delay = min(delay * 2, 120)


# ── The chain ──────────────────────────────────────────────────────────────


@dataclass
class Daily:
    """The chain over injectable effects (tests pass fakes): `exists(key)` / `count(prefix, suffix)` / `read_json(key)` on
    the data bucket (keys relative to it), `published()` the scan ids under the base's layouts, `run_job(name, spec)`,
    `prepare(scan_id)` and `prune(scan_id)` (the `daily` CLI's local stages), `list_keys(prefix)`, `log`."""
    cfg: Config
    exists: Callable[[str], bool]
    count: Callable[[str, str], int]
    read_json: Callable[[str], dict]
    published: Callable[[list[str] | None, str], list[str]]
    run_job: Callable[[str, dict], None]
    prepare: Callable[[str], None]
    prune: Callable[[str], None]
    list_keys: Callable[[str], list[str]] = lambda prefix: []
    log: Callable[[str], None] = err
    dry_run: bool = False
    verify_terms: str | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @property
    def root(self) -> str:
        return f"{PREFIX}/{self.cfg.gen}"

    def have(self) -> tuple[list[str], list[str] | None]:
        """The generation's scans (base + the newest manifest's runs), and the base's layouts."""
        base = self.read_json(f"{self.root}/scans.json")
        keys = self.manifests()
        runs = self.read_json(keys[-1])["runs"] if keys else []
        return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])], base.get("layouts")

    def manifests(self) -> list[str]:
        return sorted(k for k in self.list_keys(f"{self.root}/manifests/") if k.endswith(".json"))

    def run(self, scan_id: str, catch_up: bool = False) -> list[str]:
        """Append `scan_id` (and with `catch_up` every earlier pending scan, in order). Returns the scans appended."""
        have, layouts = self.have()
        todo = pending_scans(have, self.published(layouts, have[-1]), scan_id, catch_up)
        if not todo:
            self.log(f"{scan_id}: already appended; the R2 copy and prune only")
            if scan_id == have[-1]:
                self.r2(scan_id)
                self.stage("prune", lambda: self.prune(scan_id))
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
        self.log(f"{name}: done in {monotonic() - t:.0f}s")

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
        if self.exists(f"{run}/sidecar.parquet"):
            self.log(f"{d} shards: done")
        else:
            jobs.append(self.job("shards", d, 1, "static_append", ["shards", *g]))
        if self.exists(f"{run}/catalog/meta.json"):
            self.log(f"{d} catalog: done")
        else:
            jobs.append(self.job("catalog", d, 1, "static_append", ["catalog", *g]))
        if jobs:
            def both():
                with ThreadPoolExecutor(len(jobs)) as ex:
                    for f in [ex.submit(self.run_job, name, spec) for name, spec in jobs]:
                        f.result()
            self.stage(f"{d} shards ∥ catalog", both)
        # 4. publish: the binary counter's merges, then `manifests/<d>.json` (written once).
        if self.exists(f"{self.root}/manifests/{d}.json"):
            self.log(f"{d} publish: done")
        else:
            name, spec = self.job("publish", d, 1, "static_append", ["publish", *g])
            self.stage(f"{d} publish", lambda: self.run_job(name, spec))
        # 5. R2: the runs the manifest lists, then the manifest, last.
        self.r2(d)
        # 6. verify (optional): brute force from the scan file vs the base + runs.
        if self.verify_terms:
            if self.exists(f"{run}/verify.json"):
                self.log(f"{d} verify: done")
            else:
                name, spec = self.job("verify", d, 1, "static_append", ["verify", *g, "-t", self.verify_terms])
                self.stage(f"{d} verify", lambda: self.run_job(name, spec))
        # 7. prune: only the newest complete open-version state is kept.
        self.stage(f"{d} prune", lambda: self.prune(d))

    def r2(self, d: str) -> None:
        """One job: `r2-copy` of each run the manifest lists, then of `manifests/` (each skips what R2 already holds)."""
        key = f"{self.root}/manifests/{d}.json"
        runs = [r["key"] for r in self.read_json(key)["runs"]] if self.exists(key) else [f"deltas/{d} (and the merges publish makes)"]
        if self.dry_run:
            self.log(f"{d} r2: would copy {', '.join(runs)}, then manifests/")
            return
        cmds = [task_command(self.cfg, "static_names", ["r2-copy", "-g", f"{self.cfg.gen}/{r}"], mount=False) for r in runs]
        cmds.append(task_command(self.cfg, "static_names", ["r2-copy", "-g", self.cfg.gen, "-o", "manifests/"], mount=False))
        name = job_id("r2", d, self.now())
        spec = job_spec(self.cfg, name, 1, cmds, stage="r2", scratch=False, r2=True, machine="n2-highmem-4", ssd_gb=375)
        self.stage(f"{d} r2 ({len(runs)} runs + manifests/)", lambda: self.run_job(name, spec))


def gcs_daily(cfg: Config, *, dry_run: bool = False, verify_terms: str | None = None) -> Daily:
    """`Daily` over the real data bucket, Batch, and the `daily` CLI's `prepare` / `prune`."""
    from google.cloud import storage

    from . import static_append as sa
    from .static_names import list_scans, read_json

    client = storage.Client(project=cfg.project)
    b = client.bucket(cfg.bucket)

    def published(layouts, start):
        return [s["id"] for s in list_scans(cfg.bucket, layouts=layouts or GCS_LAYOUTS, start=start)["scans"]]

    def prepare(d):
        try:
            sa.prepare_cmd.callback(bucket=cfg.bucket, date=d, gen=cfg.gen)
        except SystemExit as e:
            raise NotNext(str(e)) from e

    def prune(d):
        k = read_json(f"gs://{cfg.bucket}/{PREFIX}/{cfg.gen}/ranges.json")["k"]
        doc = sa.prune_state(client, cfg.gen, d, k, bucket=cfg.bucket, dry_run=dry_run)
        err(json.dumps(doc))

    return Daily(
        cfg=cfg,
        exists=lambda key: b.blob(key).exists(),
        count=lambda prefix, suffix: sum(1 for x in client.list_blobs(cfg.bucket, prefix=prefix) if x.name.endswith(suffix)),
        read_json=lambda key: read_json(f"gs://{cfg.bucket}/{key}"),
        published=published,
        run_job=BatchRunner(cfg, err),
        prepare=prepare,
        prune=prune,
        dry_run=dry_run,
        verify_terms=verify_terms,
        list_keys=lambda prefix: [x.name for x in client.list_blobs(cfg.bucket, prefix=prefix)],
    )


@command("run")
@option("-c", "--catch-up", is_flag=True, help="Append every earlier published scan still pending first, in scan-id order")
@option("-g", "--gen", help="Base generation (default: $STATIC_NAMES_GEN)")
@option("-n", "--dry-run", is_flag=True, help="Report each stage's state and what would run; submit and write nothing")
@option("-t", "--verify-terms", help="Also run `daily verify` with this terms file (a gs:// URL)")
@argument("scan_id")
def run_cmd(catch_up: bool, gen: str | None, dry_run: bool, verify_terms: str | None, scan_id: str) -> None:
    """Append SCAN_ID to the static name index: prepare → append → shards ∥ catalog → publish → R2 → prune, each stage
    skipped when its output exists. Exit 3 when SCAN_ID is not published yet, or an earlier published scan is pending
    (without -c)."""
    cfg = Config.from_env(gen=gen)
    daily = gcs_daily(cfg, dry_run=dry_run, verify_terms=verify_terms)
    try:
        done = daily.run(scan_id, catch_up=catch_up)
    except NotNext as e:
        err(f"static-names daily run {scan_id}: {e}")
        sys.exit(NOT_NEXT)
    except RuntimeError as e:
        err(f"static-names daily run {scan_id}: {e}")
        sys.exit(1)
    print(json.dumps({"gen": cfg.gen, "scan": scan_id, "appended": done, "dry_run": dry_run}))
