"""cw-s3's digest config file (`job/digest.yml`) is exactly the `cw` preset:
loading it yields the same `DigestConfig`, it sets every field on its own (so
the preset can leave `cloud`), and `cw-digest -n` renders byte-identically
with and without `-C job/digest.yml`."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import yaml
from test_digest_golden import GOLDEN, _dry_run, cw_root

from dt_cloud import digest as DG

CONFIG = Path(__file__).parents[2] / "job" / "digest.yml"


def test_config_is_the_preset():
    assert DG.load_config("cw", CONFIG) == DG.PRESETS["cw"]
    assert DG.load_config("gcs", CONFIG) == DG.PRESETS["cw"]  # the file names its template


def test_config_is_complete():
    # overlaid on a base where every field is junk, the file alone still yields the preset
    junk = DG.DigestConfig(**{f.name: f"<{f.name}>" for f in fields(DG.DigestConfig)})
    assert sorted(yaml.safe_load(CONFIG.read_text())) == sorted(f.name for f in fields(DG.DigestConfig))
    assert DG.config_from_dict(yaml.safe_load(CONFIG.read_text()), junk) == DG.PRESETS["cw"]


def test_dry_run_same_with_config(monkeypatch, tmp_path: Path):
    root = cw_root(tmp_path)
    args = ["cw-digest", "-n", "-r", str(root), "-m", "2026-09"]
    for d in "abc":
        (tmp_path / d).mkdir()
    bare = _dry_run(monkeypatch, tmp_path / "a", args)
    assert bare == (GOLDEN / "cw-dry-run-sender.txt").read_text()
    assert _dry_run(monkeypatch, tmp_path / "b", ["digest", "-T", "cw", "-C", str(CONFIG), *args[1:]]) == bare
    assert _dry_run(monkeypatch, tmp_path / "c", [*args, "-C", str(CONFIG)]) == bare
