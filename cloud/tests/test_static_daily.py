"""`dt_cloud.static_daily`: the per-scan append chain's order (strictly by scan id, catching up on request), its stage
skipping (a rerun resumes at the first missing output), and the Batch jobs it submits."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dt_cloud import static_daily as sd

GEN = "2026-10-09cw"
ROOT = f"static-names/{GEN}"
CFG = sd.Config(gen=GEN, project="proj", image="img@sha256:0", sa="build@proj", r2_bucket="idx", bucket="data", scratch="scr",
                r2_sa="copy@proj", r2_secrets={"R2_ENDPOINT": "s-end", "R2_ACCESS_KEY_ID": "s-id", "R2_SECRET_ACCESS_KEY": "s-key"},
                append_tasks=4)
NOW = datetime(2026, 10, 9, 12, 34, 56, tzinfo=timezone.utc)


@pytest.mark.parametrize("have, published, scan, catch_up, want", [
    (["2026-10-08"], ["2026-10-09T1236"], "2026-10-09T1236", False, ["2026-10-09T1236"]),
    # gcs's real order: a sub-daily scan, then the cron's bare-date scan of the next day
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-10", True, ["2026-10-09T1236", "2026-10-10"]),
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-09T1236", False, ["2026-10-09T1236"]),
    # two scans on one day, then a third that is published but not asked for
    (["2026-10-08T1801"], ["2026-10-09T0001", "2026-10-09T0601", "2026-10-09T1202"], "2026-10-09T0601", True, ["2026-10-09T0001", "2026-10-09T0601"]),
    (["2026-10-08", "2026-10-09T0001"], ["2026-10-09T0601"], "2026-10-09T0001", False, []),
])
def test_pending_scans(have, published, scan, catch_up, want):
    assert sd.pending_scans(have, published, scan, catch_up) == want


@pytest.mark.parametrize("have, published, scan, msg", [
    (["2026-10-09"], ["2026-10-09T1236", "2026-10-10"], "2026-10-10",
     "1 earlier published scan(s) pending: 2026-10-09T1236 (append them first, or -c)"),
    (["2026-10-09"], ["2026-10-09T1236"], "2026-10-10", "2026-10-10 is not published (no path sort under the generation's layouts)"),
    (["2026-10-08", "2026-10-09T1236"], [], "2026-10-09T0600",
     "2026-10-09T0600 precedes the generation's newest scan 2026-10-09T1236 but was never appended: it can't join now"),
])
def test_pending_scans_refuses(have, published, scan, msg):
    with pytest.raises(sd.NotNext) as e:
        sd.pending_scans(have, published, scan, False)
    assert str(e.value) == msg


class Fake:
    """The data bucket as a dict of keys → JSON (or None), Batch as a recorder whose jobs write their stage's outputs."""

    def __init__(self, keys: dict, published: list[str], k: int = 8):
        self.keys = {f"{ROOT}/scans.json": {"scans": [{"id": "2026-10-08T1801"}]}, f"{ROOT}/ranges.json": {"k": k}, **keys}
        self.pub, self.calls = published, []

    def runs(self, d: str) -> list[dict]:
        ms = sorted(k for k in self.keys if k.startswith(f"{ROOT}/manifests/"))
        prev = self.keys[ms[-1]]["runs"] if ms else []
        return [*prev, {"key": f"deltas/{d}", "scans": [d]}]

    def run_job(self, name: str, spec: dict) -> None:
        stage = spec["labels"]["stage"]
        self.calls.append((stage, spec["taskGroups"][0]["taskCount"], spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]))
        words = self.calls[-1][2].split()
        d = words[words.index("-d") + 1] if "-d" in words else None
        out = {"append": [f"{ROOT}/deltas/{d}/dhist/r{i:04d}.parquet" for i in range(8)], "shards": [f"{ROOT}/deltas/{d}/sidecar.parquet"],
               "catalog": [f"{ROOT}/deltas/{d}/catalog/meta.json"]}.get(stage, [])
        for key in out:
            self.keys[key] = None
        if stage == "publish":
            self.keys[f"{ROOT}/manifests/{d}.json"] = {"runs": self.runs(d)}

    def daily(self, **kw) -> sd.Daily:
        return sd.Daily(
            cfg=CFG, exists=lambda key: key in self.keys, count=lambda p, s: sum(1 for x in self.keys if x.startswith(p) and x.endswith(s)),
            read_json=lambda key: self.keys[key], published=lambda layouts, start: [s for s in self.pub if s > start],
            run_job=self.run_job, prepare=lambda d: self.calls.append(("prepare", d)) or self.keys.__setitem__(f"{ROOT}/deltas/{d}/scans.json", None),
            prune=lambda d: self.calls.append(("prune", d)), list_keys=lambda p: [x for x in self.keys if x.startswith(p)],
            log=lambda m: None, now=lambda: NOW, **kw)


def _py(module: str, *args: str, mount: bool = True) -> str:
    m = " -m /gcs/data" if mount else ""
    return f"set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.{module} {' '.join(args)}{m}"


def _chain(d: str, runs: list[str]) -> list[tuple]:
    g = f"-g {GEN} -d {d}"
    r2 = " && ".join(f"( {_py('static_names', 'r2-copy', '-g', f'{GEN}/deltas/{r}', mount=False)} )" for r in runs)
    r2 += f" && ( {_py('static_names', 'r2-copy', '-g', GEN, '-o', 'manifests/', mount=False)} )"
    return [
        ("prepare", d),
        ("append", 4, _py("static_append", "append", g, "-n", "2")),
        ("shards", 1, _py("static_append", "shards", g)),
        ("catalog", 1, _py("static_append", "catalog", g)),
        ("publish", 1, _py("static_append", "publish", g)),
        ("r2", 1, r2),
        ("prune", d),
    ]


def test_one_scan_runs_every_stage_in_order():
    f = Fake({}, ["2026-10-09T0001"])
    assert f.daily().run("2026-10-09T0001") == ["2026-10-09T0001"]
    assert f.calls == _chain("2026-10-09T0001", ["2026-10-09T0001"])


def test_catch_up_appends_each_pending_scan_in_order():
    """Two scans of one day are two runs: each its own chain, the second's manifest listing both."""
    f = Fake({}, ["2026-10-09T0001", "2026-10-09T0601"])
    with pytest.raises(sd.NotNext):
        f.daily().run("2026-10-09T0601")
    assert f.calls == []
    assert f.daily().run("2026-10-09T0601", catch_up=True) == ["2026-10-09T0001", "2026-10-09T0601"]
    assert f.calls == [*_chain("2026-10-09T0001", ["2026-10-09T0001"]), *_chain("2026-10-09T0601", ["2026-10-09T0001", "2026-10-09T0601"])]


def test_a_rerun_resumes_at_the_first_missing_output():
    d = "2026-10-09T0001"
    done = {f"{ROOT}/deltas/{d}/scans.json": None, **{f"{ROOT}/deltas/{d}/dhist/r{i:04d}.parquet": None for i in range(8)},
            f"{ROOT}/deltas/{d}/catalog/meta.json": None}
    f = Fake(done, [d])
    f.daily().run(d)
    chain = _chain(d, [d])
    assert f.calls == [chain[2], *chain[4:]]


def test_an_appended_scan_reruns_only_the_r2_copy_and_prune():
    d = "2026-10-09T0001"
    f = Fake({f"{ROOT}/manifests/{d}.json": {"runs": [{"key": f"deltas/{d}", "scans": [d]}]}}, [d])
    assert f.daily().run(d) == []
    assert f.calls == _chain(d, [d])[5:]


def test_dry_run_submits_nothing():
    f = Fake({}, ["2026-10-09T0001"])
    f.daily(dry_run=True).run("2026-10-09T0001")
    assert f.calls == []


def test_r2_job_spec():
    """The R2 copy: as the copy account, its credentials from Secret Manager, no scratch mount, chained copies."""
    cmds = [_py("static_names", "r2-copy", "-g", GEN, mount=False)]
    spec = sd.job_spec(CFG, "sn-r2-x", 1, cmds, stage="r2", scratch=False, r2=True, machine="n2-highmem-4", ssd_gb=375)
    assert spec == {
        "taskGroups": [{
            "taskCount": 1, "parallelism": 1,
            "taskSpec": {
                "runnables": [{"container": {"imageUri": "img@sha256:0", "entrypoint": "bash", "commands": ["-c", cmds[0]],
                                             "volumes": ["/mnt/disks/gcs/data:/gcs/data:ro", "/mnt/disks/stage:/stage:rw"]}}],
                "environment": {
                    "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "R2_BUCKET": "idx"},
                    "secretVariables": {"R2_ACCESS_KEY_ID": "projects/proj/secrets/s-id/versions/latest",
                                        "R2_ENDPOINT": "projects/proj/secrets/s-end/versions/latest",
                                        "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/s-key/versions/latest"},
                },
                "computeResource": {"cpuMilli": 4000, "memoryMib": 30800},
                "maxRetryCount": 3, "maxRunDuration": "14400s",
                "volumes": [{"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data", "mountOptions": ["--implicit-dirs"]},
                            {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "n2-highmem-4", "provisioningModel": "SPOT", "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                                      "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": "375"}, "deviceName": "stage"}]}}],
            "serviceAccount": {"email": "copy@proj"},
            "location": {"allowedLocations": ["regions/us-east1"]},
        },
        "labels": {"purpose": "static-names", "stage": "r2", "gen": "2026-10-09cw"},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def test_task_command_from_mounted_source():
    cfg = sd.Config(**{**CFG.__dict__, "src": ("/gcs/data/static-names/src/t/dt_cloud", "/gcs/data/static-names/src/pyrmts-r/pyrmts")})
    assert sd.task_command(cfg, "static_append", ["shards", "-d", "2026-10-09T0001"]) == (
        "set -euo pipefail; mkdir -p /stage/tmp /stage/out /stage/src && cp -r /gcs/data/static-names/src/t/dt_cloud "
        "/gcs/data/static-names/src/pyrmts-r/pyrmts /stage/src/ && cd /stage && PYTHONPATH=/stage/src python3 -u -m dt_cloud.static_append "
        "shards -d 2026-10-09T0001 -m /gcs/data")


def test_job_ids_are_batch_safe():
    assert [sd.job_id("append", s, NOW) for s in ("2026-10-09T1236", "2026-10-10")] == ["sn-append-2026-10-09-1236-123456", "sn-append-2026-10-10-123456"]


def test_config_from_env(monkeypatch):
    """cw's deployment config: the build account, the scan job's R2 secrets for the copy as its own account."""
    for k, v in {"STATIC_NAMES_GEN": GEN, "STATIC_NAMES_IMAGE": "img", "STATIC_NAMES_SA": "build@p", "STATIC_NAMES_R2_SA": "cw@p",
                 "R2_BUCKET": "idx", "STATIC_NAMES_R2_SECRETS": "endpoint=cw-r2-endpoint,key_id=cw-r2-id,secret=cw-r2-key",
                 "STATIC_NAMES_SPOT": "0", "STATIC_NAMES_SRC": "/gcs/a/x,/gcs/a/y"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    cfg = sd.Config.from_env(project="p")
    assert (cfg.gen, cfg.image, cfg.sa, cfg.r2_sa, cfg.r2_bucket, cfg.r2_secrets, cfg.spot, cfg.src, cfg.region) == (
        GEN, "img", "build@p", "cw@p", "idx",
        {"R2_ENDPOINT": "cw-r2-endpoint", "R2_ACCESS_KEY_ID": "cw-r2-id", "R2_SECRET_ACCESS_KEY": "cw-r2-key"}, False, ("/gcs/a/x", "/gcs/a/y"), "us-east1")
    monkeypatch.setenv("STATIC_NAMES_R2_SECRETS", "endpoint=e,key_id=i")
    with pytest.raises(SystemExit) as e:
        sd.Config.from_env(project="p")
    assert str(e.value) == "STATIC_NAMES_R2_SECRETS needs secret=<secret name>"
