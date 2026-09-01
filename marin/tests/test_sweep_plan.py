"""Fate-resolution spec for the sweep planner (specs/sweep-executor.md).

Each test states the ledger and the exact expected fates — these are the
semantics `/api/resolve` implements in SQL and the site implements in TS;
divergence here means the planner must not run.
"""

from __future__ import annotations

from gcs_usage.sweep_plan import (
    FateResolver,
    KeepRow,
    KlcSplit,
    key_to_prefixes,
    klc_key_fate,
    klc_split,
    load_keeps,
)

B = "gs://marin-us-east5/"


def _r(prefix: str, keep, ts: int, aid: int) -> KeepRow:
    return KeepRow(prefix=prefix, keep=keep, ts=ts, action_id=aid, who="t@oa")


def test_key_to_prefixes():
    assert key_to_prefixes("marin-b", "x/y/f.bin") == [
        "gs://marin-b/",
        "gs://marin-b/x/",
        "gs://marin-b/x/y/",
    ]
    assert key_to_prefixes("marin-b", "f.bin") == ["gs://marin-b/"]


def test_recency_beats_specificity():
    """A newer broad mark repaints an older deeper one."""
    fr = FateResolver([
        _r(f"{B}run/ckpt/", "keep", ts=100, aid=1),
        _r(f"{B}run/", "sweep", ts=200, aid=2),
    ])
    assert fr.fate(f"{B}run/ckpt/") == "sweep"
    assert fr.fate(f"{B}run/") == "sweep"
    assert fr.fate(f"{B}other/") is None


def test_child_carve_out_after_parent():
    """Marking the child *after* the parent carves the exception."""
    fr = FateResolver([
        _r(f"{B}run/", "sweep", ts=100, aid=1),
        _r(f"{B}run/gold/", "keep", ts=200, aid=2),
    ])
    assert fr.fate(f"{B}run/gold/") == "keep"
    assert fr.fate(f"{B}run/") == "sweep"
    assert fr.fate(f"{B}run/junk/") == "sweep"  # covered by the parent band


def test_explicit_unmark_wins():
    fr = FateResolver([
        _r(f"{B}d/", "sweep", ts=100, aid=1),
        _r(f"{B}d/", None, ts=200, aid=2),
    ])
    assert fr.fate(f"{B}d/") is None


def test_ts_tie_breaks_on_action_id():
    fr = FateResolver([
        _r(f"{B}d/", "keep", ts=100, aid=1),
        _r(f"{B}d/", "sweep", ts=100, aid=2),
    ])
    assert fr.fate(f"{B}d/") == "sweep"


def test_key_fate_deepest_marked_ancestor_with_provenance():
    """The deepest marked ancestor decides; the winning row (provenance) may
    sit on a shallower prefix when recency repainted it."""
    deep = _r(f"{B}run/ckpt/", "keep", ts=100, aid=1)
    broad = _r(f"{B}run/", "sweep", ts=200, aid=2)
    fr = FateResolver([deep, broad])
    fate, row = fr.key_fate("marin-us-east5", "run/ckpt/model.bin")
    assert (fate, row) == ("sweep", broad)
    fate, row = fr.key_fate("marin-us-east5", "unmarked/f.bin")
    assert (fate, row) == (None, None)


def test_load_keeps_shapes_api_rows():
    payload = {"keeps": [
        {"prefix": f"{B}a/", "keep": "sweep", "ts": 5, "action_id": 9, "who": "x@oa", "memo": None},
        {"prefix": f"{B}b/", "keep": None, "ts": 6, "action_id": 10},
    ]}
    assert load_keeps(payload) == [
        KeepRow(prefix=f"{B}a/", keep="sweep", ts=5, action_id=9, who="x@oa"),
        KeepRow(prefix=f"{B}b/", keep=None, ts=6, action_id=10),
    ]


# ---- KLC ------------------------------------------------------------------

def test_klc_split_hf_checkpoints_numeric_max():
    """`checkpoint-N` layout: numeric max (checkpoint-10000 > checkpoint-9000),
    non-step siblings at that level sweep — matching the site's klcSplits."""
    split = klc_split([
        "checkpoint-9000/model.safetensors",
        "checkpoint-10000/model.safetensors",
        "checkpoint-10000/optimizer.pt",
        "config.json",
        "logs/train.log",
    ])
    assert split == KlcSplit(prefix="", kept=("checkpoint-10000/",), resolved=True)
    assert klc_key_fate("checkpoint-10000/model.safetensors", split) == "keep"
    assert klc_key_fate("checkpoint-9000/model.safetensors", split) == "sweep"
    assert klc_key_fate("config.json", split) == "sweep"
    assert klc_key_fate("logs/train.log", split) == "sweep"


def test_klc_split_nested_runs_recurse():
    """Steps one level down: each run dir gets its own max-step keep."""
    split = klc_split([
        "run-a/step_100/w.bin",
        "run-a/step_200/w.bin",
        "run-b/global_step50/w.bin",
        "run-b/global_step150/w.bin",
    ])
    assert sorted(split.kept) == ["run-a/step_200/", "run-b/global_step150/"]
    assert split.resolved is True
    assert klc_key_fate("run-a/step_200/w.bin", split) == "keep"
    assert klc_key_fate("run-a/step_100/w.bin", split) == "sweep"
    assert klc_key_fate("run-b/global_step150/w.bin", split) == "keep"


def test_klc_split_no_steps_is_unresolved_and_keeps():
    split = klc_split(["data/part-0.parquet", "data/part-1.parquet"])
    assert split == KlcSplit(prefix="", kept=(), resolved=False)
    assert klc_key_fate("data/part-0.parquet", split) == "keep"


def test_klc_split_stops_at_first_step_level():
    """A node with step children does NOT recurse — deeper step dirs inside the
    kept subtree stay kept wholesale (exactly the tree walk's `return`)."""
    split = klc_split([
        "step-1/inner/step-99/w.bin",
        "step-2/inner/step-1/w.bin",
    ])
    assert split.kept == ("step-2/",)
    assert klc_key_fate("step-2/inner/step-1/w.bin", split) == "keep"
    assert klc_key_fate("step-1/inner/step-99/w.bin", split) == "sweep"
