"""cw-s3's `job/scan-runs.json`: its static output follows the cw static-names profile's live generation."""
from __future__ import annotations

import json
from pathlib import Path

from dt_cloud.scan_runs import fill
from dt_cloud.static_profile_examples import EXAMPLES

PROFILE = Path(__file__).parents[2] / "job/scan-runs.json"


def test_cw_profile_static_output_tracks_the_cw_generation(monkeypatch):
    monkeypatch.delenv("STATIC_NAMES_GEN", raising=False)
    [static] = [o for o in json.loads(PROFILE.read_text())["outputs"] if o["key"] == "static"]
    assert fill(static["uri"], "2026-10-10T1201", None) == (
        f"gs://oa-gcs-usage-dvx/static-names/{EXAMPLES['cw'].gen}/manifests/2026-10-10T1201.json"
    )
