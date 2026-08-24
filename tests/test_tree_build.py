"""`disk_tree.tree_build.build_tree` — the shared arbitrary-depth tree linker.

Pins the parent→child linking, the relative-floor fold (with the `f` marker
that lets the client expand `(other)`), and the additive re-combination of
class/date/attribution onto fold nodes. These are the invariants both the GCS
and CoreWeave webdata paths now depend on.
"""

from __future__ import annotations

from disk_tree.tree_build import DirRow, build_tree


def test_links_arbitrary_depth_parent_to_child():
    # Bytes flow straight down (no direct files at any level → no remainder),
    # so this isolates the linking: c sits three levels below the root, uncapped.
    tree = build_tree([
        DirRow(".", 70, 4),
        DirRow("a", 70, 4),
        DirRow("a/b", 70, 4),
        DirRow("a/b/c", 70, 4),
    ], total_bytes=70, min_frac=0.0)
    assert tree == {
        "n": ".", "b": 70, "o": 4,
        "c": [{"n": "a", "b": 70, "o": 4,
               "c": [{"n": "b", "b": 70, "o": 4,
                      "c": [{"n": "c", "b": 70, "o": 4}]}]}],
    }


def test_children_sorted_by_bytes_desc():
    tree = build_tree([
        DirRow(".", 100, 3),
        DirRow("small", 10, 1),
        DirRow("big", 80, 1),
        DirRow("mid", 10, 1),
    ], total_bytes=100, min_frac=0.0)
    assert [c["n"] for c in tree["c"]] == ["big", "small", "mid"]  # ties keep input order


def test_subfloor_children_fold_into_marked_other():
    # Floor = 20. `keep` (50) survives; two 5-byte dirs fold; the (other) also
    # absorbs the 20 bytes of direct files the parent holds beyond its kids.
    tree = build_tree([
        DirRow(".", 80, 10),
        DirRow("keep", 50, 4),
        DirRow("x", 5, 2),
        DirRow("y", 5, 2),
    ], total_bytes=80, min_frac=0.25)
    assert tree["c"] == [
        {"n": "keep", "b": 50, "o": 4},
        {"n": "(other)", "b": 30, "o": 6, "f": 2},
    ]


def test_no_other_when_remainder_is_dust():
    # Remainder after `keep` is 5 (< floor 20): dropped, not folded.
    tree = build_tree([
        DirRow(".", 55, 5),
        DirRow("keep", 50, 4),
        DirRow("tiny", 5, 1),
    ], total_bytes=55, min_frac=0.36)
    assert tree["c"] == [{"n": "keep", "b": 50, "o": 4}]


def test_leaf_parent_gets_no_c_key():
    tree = build_tree([DirRow(".", 40, 4), DirRow("solo", 40, 4)], total_bytes=40, min_frac=0.0)
    assert tree["c"] == [{"n": "solo", "b": 40, "o": 4}]
    assert "c" not in tree["c"][0]


def test_carries_extra_fields_onto_nodes():
    tree = build_tree([
        DirRow(".", 100, 2, {"cb": {"3": 100}, "tm": {"OA": 100}}),
        DirRow("a", 100, 2, {"cb": {"3": 100}, "tm": {"OA": 100}}),
    ], total_bytes=100, min_frac=0.0)
    assert tree["c"][0] == {"n": "a", "b": 100, "o": 2, "cb": {"3": 100}, "tm": {"OA": 100}}


def test_other_recombines_class_date_and_attribution():
    # floor = 0.35 × 1000 = 350. `keep` (400) survives; f1/f2 (100/300) fold.
    # The (other) sums them and byte-weights their means: 100@day10, 300@day30
    # → (10·100 + 30·300)/400 = day25.
    tree = build_tree([
        DirRow(".", 1000, 20),
        DirRow("keep", 400, 4, {"tm": {"OA": 400}}),
        DirRow("f1", 100, 5, {"d": 10, "cb": {"2": 40}, "tm": {"OA": 100}, "us": [["ryan", 100]]}),
        DirRow("f2", 300, 6, {"d": 30, "cb": {"2": 60, "3": 300}, "tm": {"Stanford": 300}, "sh": {"Stanford": 300}}),
    ], total_bytes=1000, min_frac=0.35)
    assert tree["c"][0] == {"n": "keep", "b": 400, "o": 4, "tm": {"OA": 400}}
    other = tree["c"][-1]
    # rest_b = 1000 − 400(keep) = 600 (f1 100 + f2 300 + 200 direct files).
    assert (other["n"], other["b"], other["o"], other["f"]) == ("(other)", 600, 16, 2)
    assert other["d"] == 25
    assert other["cb"] == {"3": 300, "2": 100}
    assert other["tm"] == {"Stanford": 300, "OA": 100}
    assert other["sh"] == {"Stanford": 300}
    assert other["us"] == [["ryan", 100]]
