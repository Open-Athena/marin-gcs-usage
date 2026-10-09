"""A deployment's static name index config (`Profile`): where its scans are, where the index goes, how its Batch jobs
run. Nothing here assumes a deployment: the generic stages read the profile, and missing config is an error naming
the variable to set.

Loaded (`load_profile`) from `STATIC_NAMES_PROFILE` — a named example (`static_profile_examples.EXAMPLES`: `gcs`,
`cw`) or a JSON file of `Profile` fields — then each field's env var on top (`ENV`), so a deployment can select an
example, check in its own file, or set every field by env.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from functools import cache

#: The R2 copy's credentials: Secret Manager secret names, by the env var each sets in the copy job.
R2_SECRET_VARS = {"endpoint": "R2_ENDPOINT", "key_id": "R2_ACCESS_KEY_ID", "secret": "R2_SECRET_ACCESS_KEY"}


@dataclass(frozen=True)
class Profile:
    name: str = ""
    #: Key templates of the scans' `path` sorts in the data bucket: `{id}` the scan id, `{gen}` the store generation
    #: (a scan's newest wins; a template without one counts as the oldest), e.g. `cw-l2/{id}/index/{gen}/path-index.parquet`.
    layouts: tuple[str, ...] = ()
    #: The data bucket: the scans, and the generations under `static-names/<gen>/`.
    bucket: str | None = None
    #: Intermediates (the suffix shuffle, markers, the open-version state): no soft delete, a short age-based delete rule.
    scratch: str | None = None
    #: The base generation the runs append to.
    gen: str | None = None
    #: The R2 bucket serving the index (`static-names/<gen>/` there too).
    r2_bucket: str | None = None
    #: `R2_SECRET_VARS` keys → Secret Manager secret names; `r2_endpoint` (plain) may stand in for the endpoint's.
    r2_secrets: Mapping[str, str] = field(default_factory=dict)
    r2_endpoint: str | None = None
    project: str | None = None
    region: str | None = None
    #: The job image (`dt_cloud` + `pyrmts`).
    image: str | None = None
    #: The stages' account (the data and scratch buckets); the R2 copy's (its secrets), default the stages'.
    sa: str | None = None
    r2_sa: str | None = None
    machine: str = "n2-highmem-16"
    ssd_gb: int = 750
    spot: bool = True
    append_tasks: int = 16
    #: Whether each run also gets the heavy-term drilldown (`drill/`, `static_drill`): needs the base generation's `drill/`.
    drill: bool = False
    #: Mounted dirs copied onto the tasks' PYTHONPATH in place of the image's own code (a staged tree).
    src: tuple[str, ...] = ()

    def need(self, name: str):
        v = getattr(self, name)
        if v in (None, "", ()):
            raise SystemExit(f"static names: no {name} in the deployment profile: set {ENV[name]} (or STATIC_NAMES_PROFILE)")
        return v

    def r2_env_secrets(self) -> dict[str, str]:
        """`{env var: secret name}` for the R2 copy, checked complete."""
        out = {R2_SECRET_VARS[k]: v for k, v in self.r2_secrets.items()}
        if "R2_ENDPOINT" not in out and not self.r2_endpoint:
            raise SystemExit("static names: the R2 endpoint: set R2_ENDPOINT, or name its secret in STATIC_NAMES_R2_SECRETS (endpoint=…)")
        for part, k in (("key_id", "R2_ACCESS_KEY_ID"), ("secret", "R2_SECRET_ACCESS_KEY")):
            if k not in out:
                raise SystemExit(f"static names: STATIC_NAMES_R2_SECRETS needs {part}=<secret name>")
        return out


#: Each field's env var.
ENV = {
    "name": "STATIC_NAMES_PROFILE", "layouts": "STATIC_NAMES_LAYOUTS", "bucket": "STATIC_NAMES_BUCKET", "scratch": "STATIC_NAMES_SCRATCH",
    "gen": "STATIC_NAMES_GEN", "r2_bucket": "R2_BUCKET", "r2_secrets": "STATIC_NAMES_R2_SECRETS", "r2_endpoint": "R2_ENDPOINT",
    "project": "GCP_PROJECT", "region": "STATIC_NAMES_REGION", "image": "STATIC_NAMES_IMAGE", "sa": "STATIC_NAMES_SA",
    "r2_sa": "STATIC_NAMES_R2_SA", "machine": "STATIC_NAMES_MACHINE", "ssd_gb": "STATIC_NAMES_SSD", "spot": "STATIC_NAMES_SPOT",
    "append_tasks": "STATIC_NAMES_APPEND_TASKS", "drill": "STATIC_NAMES_DRILL", "src": "STATIC_NAMES_SRC",
}


def _parse(name: str, raw: str):
    if name in ("layouts", "src"):
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    if name == "r2_secrets":
        out = {}
        for part in filter(None, (p.strip() for p in raw.split(","))):
            k, _, v = part.partition("=")
            if k.strip() not in R2_SECRET_VARS or not v.strip():
                raise SystemExit(f"STATIC_NAMES_R2_SECRETS: {part!r} is not endpoint=|key_id=|secret=<secret name>")
            out[k.strip()] = v.strip()
        return out
    if name in ("ssd_gb", "append_tasks"):
        return int(raw)
    if name in ("spot", "drill"):
        return raw.strip() not in ("0", "false", "")
    return raw.strip()


def from_mapping(d: Mapping) -> Profile:
    """A profile from a mapping of its fields (a JSON file, an example); unknown keys are an error."""
    known = {f.name for f in fields(Profile)}
    if bad := sorted(set(d) - known):
        raise SystemExit(f"static names profile: unknown fields {bad}")
    d = dict(d)
    for k in ("layouts", "src"):
        if k in d:
            d[k] = tuple(d[k])
    return Profile(**d)


def load_profile(env: Mapping[str, str] | None = None) -> Profile:
    """`STATIC_NAMES_PROFILE` (an example's name or a JSON file), then every field's env var over it."""
    from .static_profile_examples import EXAMPLES

    env = os.environ if env is None else env
    base = Profile()
    sel = (env.get("STATIC_NAMES_PROFILE") or "").strip()
    if sel in EXAMPLES:
        base = EXAMPLES[sel]
    elif sel:
        if not os.path.exists(sel):
            raise SystemExit(f"STATIC_NAMES_PROFILE: {sel!r} is neither an example ({', '.join(sorted(EXAMPLES))}) nor a file")
        with open(sel) as fh:
            base = from_mapping({"name": os.path.basename(sel), **json.load(fh)})
    over = {k: _parse(k, env[v]) for k, v in ENV.items() if k != "name" and (env.get(v) or "").strip()}
    return replace(base, **over)


@cache
def profile() -> Profile:
    """The process's profile (`load_profile` over the environment, once)."""
    return load_profile()


def data_bucket() -> str:
    return profile().need("bucket")


def scratch_bucket() -> str:
    return profile().need("scratch")


def layouts() -> tuple[str, ...]:
    return profile().need("layouts")
