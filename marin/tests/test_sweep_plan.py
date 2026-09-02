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

# ---- clobbered keeps + ever-kept guard ------------------------------------

def test_clobbered_keeps_broad_sweep_incident():
    """The 2026-09-01 shape: a whole-checkpoints/ sweep repaints two earlier
    keeps; a later carve-out keep is NOT clobbered."""
    from gcs_usage.sweep_plan import Clobber, clobbered_keeps
    rows = [
        _r(f"{B}checkpoints/calvin/", "keep", ts=100, aid=1),
        _r(f"{B}checkpoints/pinlin_calvin_xu/", "keep", ts=110, aid=2),
        KeepRow(prefix=f"{B}checkpoints/", keep="sweep", ts=200, action_id=3, who="p@x"),
        _r(f"{B}checkpoints/gold/", "keep", ts=300, aid=4),  # re-kept after — safe
        _r(f"{B}elsewhere/", "keep", ts=50, aid=5),  # never covered by a sweep
    ]
    assert clobbered_keeps(rows) == [
        Clobber(prefix=f"{B}checkpoints/calvin/", keep="keep", keeper="t@oa", keep_ts=100,
                by_prefix=f"{B}checkpoints/", by_who="p@x", by_ts=200),
        Clobber(prefix=f"{B}checkpoints/pinlin_calvin_xu/", keep="keep", keeper="t@oa", keep_ts=110,
                by_prefix=f"{B}checkpoints/", by_who="p@x", by_ts=200),
    ]


def test_ever_kept_prefixes_ignores_repaints():
    from gcs_usage.sweep_plan import ever_kept_prefixes
    rows = [
        _r(f"{B}a/", "keep", ts=100, aid=1),
        KeepRow(prefix=f"{B}a/", keep="sweep", ts=200, action_id=2, who="p@x"),
        _r(f"{B}b/", "keep_last_ckpt", ts=100, aid=3),
        KeepRow(prefix=f"{B}c/", keep="sweep", ts=100, action_id=4, who="p@x"),
        _r(f"{B}d/", None, ts=100, aid=5),  # unmark is not a keep
    ]
    assert ever_kept_prefixes(rows) == frozenset({f"{B}a/", f"{B}b/"})

# ---- vote model (specs/vote-model.md) -------------------------------------

def test_votes_broad_sweep_does_not_clobber():
    """The 9/1 incident under the vote model: Pranshu's broad sweep + Calvin's
    earlier keep = conflict at Calvin's path, sweep-only elsewhere."""
    from gcs_usage.sweep_plan import VoteResolver
    vr = VoteResolver([
        KeepRow(prefix=f"{B}checkpoints/calvin/", keep="keep", ts=100, action_id=1, who="calvin"),
        KeepRow(prefix=f"{B}checkpoints/", keep="sweep", ts=200, action_id=2, who="pranshu"),
    ])
    assert vr.votes(f"{B}checkpoints/calvin/") == {"calvin": "keep", "pranshu": "sweep"}
    assert vr.state(f"{B}checkpoints/calvin/") == "conflict"
    assert vr.votes(f"{B}checkpoints/junk/") == {"pranshu": "sweep"}
    assert vr.state(f"{B}checkpoints/junk/") == "sweep"
    assert vr.state(f"{B}elsewhere/") == "unmarked"


def test_votes_unmark_retracts_only_own_vote():
    """Pranshu's final east5 unmark drops HIS vote; the keeps stand alone."""
    from gcs_usage.sweep_plan import VoteResolver
    vr = VoteResolver([
        KeepRow(prefix=f"{B}checkpoints/g/", keep="keep", ts=100, action_id=1, who="gonzalo"),
        KeepRow(prefix=f"{B}checkpoints/", keep="sweep", ts=200, action_id=2, who="pranshu"),
        KeepRow(prefix=f"{B}checkpoints/", keep=None, ts=300, action_id=3, who="pranshu"),
    ])
    assert vr.votes(f"{B}checkpoints/g/") == {"gonzalo": "keep"}
    assert vr.state(f"{B}checkpoints/g/") == "keep"
    assert vr.state(f"{B}checkpoints/junk/") == "unmarked"


def test_votes_self_repaint_still_works():
    """A user repainting their OWN keep to sweep yields a deletable path —
    more precise than the ever-kept guard."""
    from gcs_usage.sweep_plan import VoteResolver
    vr = VoteResolver([
        KeepRow(prefix=f"{B}mine/", keep="keep", ts=100, action_id=1, who="a"),
        KeepRow(prefix=f"{B}mine/", keep="sweep", ts=200, action_id=2, who="a"),
    ])
    assert vr.votes(f"{B}mine/") == {"a": "sweep"}
    assert vr.state(f"{B}mine/") == "sweep"


def test_key_state_per_user_granularity():
    """Per-user recency across granularity: A sweeps broad then keeps a child;
    B sweeps the child later — child is conflict, sibling sweep-only."""
    from gcs_usage.sweep_plan import VoteResolver
    vr = VoteResolver([
        KeepRow(prefix=f"{B}run/", keep="sweep", ts=100, action_id=1, who="a"),
        KeepRow(prefix=f"{B}run/gold/", keep="keep", ts=200, action_id=2, who="a"),
        KeepRow(prefix=f"{B}run/gold/", keep="sweep", ts=300, action_id=3, who="b"),
    ])
    assert vr.key_votes("marin-us-east5", "run/gold/model.bin") == {"a": "keep", "b": "sweep"}
    assert vr.key_state("marin-us-east5", "run/gold/model.bin") == "conflict"
    assert vr.key_state("marin-us-east5", "run/junk/f.bin") == "sweep"

# ---- manifest classification (policy (b)) ---------------------------------

def test_classify_dir_policy_b():
    from gcs_usage.identity import IdentityMap
    from gcs_usage.sweep_plan import VoteResolver, classify_dir, ever_kept_prefixes, owners_resolver
    idmap = IdentityMap(user_teams={}, alias_to_user={"kaiyuewen3": "kaiyue"}, teams=(), prefix_owners=())
    rows = [
        KeepRow(prefix=f"{B}grug/", keep="sweep", ts=100, action_id=1, who="kaiyuewen3@gmail.com"),
        KeepRow(prefix=f"{B}other/", keep="sweep", ts=100, action_id=2, who="kaiyuewen3@gmail.com"),
        KeepRow(prefix=f"{B}unowned/", keep="sweep", ts=100, action_id=3, who="kaiyuewen3@gmail.com"),
        KeepRow(prefix=f"{B}was-kept/", keep="keep", ts=50, action_id=4, who="kaiyuewen3@gmail.com"),
        KeepRow(prefix=f"{B}was-kept/", keep="sweep", ts=100, action_id=5, who="kaiyuewen3@gmail.com"),
        KeepRow(prefix=f"{B}klc/", keep="keep_last_ckpt", ts=100, action_id=6, who="kaiyuewen3@gmail.com"),
    ]
    owners = {"owners": [
        {"prefix": f"{B}grug/", "owner": "kaiyue", "ts": 90, "action_id": 10, "who": "kaiyuewen3@gmail.com"},
        {"prefix": f"{B}other/", "owner": "gonzalo", "ts": 90, "action_id": 11, "who": "g@oa"},
        {"prefix": f"{B}was-kept/", "owner": "kaiyue", "ts": 90, "action_id": 12, "who": "kaiyuewen3@gmail.com"},
    ]}
    vr, own = VoteResolver(rows), owners_resolver(owners)
    ever = ever_kept_prefixes(rows)
    bkt = "marin-us-east5"
    assert classify_dir(bkt, "grug/run1", vr, own, idmap, ever) == ("eligible", "kaiyue", ("kaiyue",))
    assert classify_dir(bkt, "other/x", vr, own, idmap, ever) == ("deferred_owner", "gonzalo", ("kaiyue",))
    assert classify_dir(bkt, "unowned/x", vr, own, idmap, ever) == ("deferred_unowned", None, ("kaiyue",))
    assert classify_dir(bkt, "was-kept/x", vr, own, idmap, ever) == ("ever_kept", None, ("kaiyue",))
    assert classify_dir(bkt, "klc/run", vr, own, idmap, ever) == ("klc_pending", None, ())
    assert classify_dir(bkt, "nothing/here", vr, own, idmap, ever) == ("unmarked", None, ())


def test_classify_dir_approved_bands_replace_owner_check():
    from gcs_usage.identity import IdentityMap
    from gcs_usage.sweep_plan import VoteResolver, classify_dir, ever_kept_prefixes, owners_resolver
    idmap = IdentityMap(user_teams={}, alias_to_user={}, teams=(), prefix_owners=())
    rows = [
        KeepRow(prefix=f"{B}rl_testing/", keep="sweep", ts=100, action_id=1, who="ahmed@x"),
        KeepRow(prefix=f"{B}grug/", keep="sweep", ts=100, action_id=2, who="k@x"),
    ]
    vr = VoteResolver(rows)
    own = owners_resolver({"owners": []})
    ever = ever_kept_prefixes(rows)
    approved = (f"{B}rl_testing/",)
    assert classify_dir("marin-us-east5", "rl_testing/run1", vr, own, idmap, ever, approved) == ("eligible", None, ("ahmed",))
    assert classify_dir("marin-us-east5", "grug/x", vr, own, idmap, ever, approved) == ("deferred_owner", None, ("k",))
    # without an approved list the unclaimed dirs defer entirely
    assert classify_dir("marin-us-east5", "rl_testing/run1", vr, own, idmap, ever) == ("deferred_unowned", None, ("ahmed",))
