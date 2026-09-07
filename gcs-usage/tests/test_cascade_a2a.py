"""`cascade-a2a` reads mgu's path index and DT's dirs tier as (path, usr) rows and reports every disagreement."""
from pathlib import Path

import pandas as pd

from gcs_usage.cascade_a2a import compare, render


def _write(tmp_path: Path):
    idx = tmp_path / "path-index.parquet"
    pd.DataFrame(
        {
            "path": ["b", "b", "b/x", "b/y", "b/z", "b/dd/", "other", "other/q"],
            "depth": [1, 1, 2, 2, 2, 3, 1, 2],
            "usr": [None, "kim", "kim", None, None, None, None, None],
            "b": [10, 30, 30, 10, 0, 4, 99, 99],
            "o": [1, 3, 3, 1, 0, 1, 2, 2],
            "wts": [10 * 1000.0, 30 * 2000.0, 30 * 2000.0, 10 * 1000.0, 0.0, 0.0, 0.0, 0.0],
            "wb": [10, 30, 30, 10, 0, 0, 0, 0],
            "c2": [10, 30, 30, 10, 0, 4, 99, 99],
            "c3": [0] * 8,
            "c4": [0] * 8,
            "a": [None] * 8,
        }
    ).to_parquet(idx)
    dt = tmp_path / "gcs-b.dirs.parquet"
    pd.DataFrame(
        {
            "path": [".", ".", "x", "y", "w", "e"],
            "usr": [None, "kim", "kim", None, None, "kim"],
            "size": [10, 30, 31, 10, 5, 0],
            "n_files": [1, 3, 2, 1, 1, 0],
            "mtime_mean": [1000.0, 2000.0, 2000.0, 1001.5, None, None],
            "sum_storage_class_id_2": [10, 30, 31, 10, 5, 0],
        }
    ).to_parquet(dt)
    return str(idx), str(dt)


def test_compare_reports_each_class_of_difference(tmp_path: Path):
    idx, dt = _write(tmp_path)
    r = compare("b", idx, dt, top=5)
    assert r["rows"] == {"mgu": 6, "dt": 6, "both": 4}
    assert r["root"] == {
        "mgu": [{"usr": None, "b": 10, "o": 1}, {"usr": "kim", "b": 30, "o": 3}],
        "dt": [{"usr": None, "b": 10, "o": 1}, {"usr": "kim", "b": 30, "o": 3}],
    }
    assert r["only"]["mgu"] == {"n": 1, "examples": [{"path": "z", "usr": None, "b": 0, "o": 0}]}
    assert r["only"]["dt"] == {"n": 1, "examples": [{"path": "w", "usr": None, "b": 5, "o": 1}]}
    assert r["known"] == {"double_slash_dirs": 1, "empty_slices": 1}  # `dd/` and kim's empty `e` don't fail the gate
    assert r["mismatch"]["b"] == {"n": 1, "examples": [{"path": "x", "usr": "kim", "mgu": 30, "dt": 31}]}
    assert r["mismatch"]["o"] == {"n": 1, "examples": [{"path": "x", "usr": "kim", "mgu": 3, "dt": 2}], "delta_sum": 1}
    assert r["mismatch"]["c2"] == {"n": 1, "examples": [{"path": "x", "usr": "kim", "mgu": 30, "dt": 31}]}
    assert r["mismatch"]["c3"] == {"n": 0, "examples": []}  # absent from the DT file: mgu's zeros against 0
    assert r["mismatch"]["mtime"] == {"n": 0, "examples": []}  # 1000 vs 1001.5 is within two seconds
    assert r["against_zero"] == ["c3", "c4"]
    assert r["skipped"] == []
    assert r["ok"] is False
    assert render(r).splitlines()[0] == "b: rows mgu=6 dt=6 both=4"
    assert render(r).splitlines()[-1] == "  DIFFERENT"
    assert render(r).splitlines()[-3] == "  known one-sided: 1 mgu `a//b` dir rows, 1 DT empty slices"


def test_compare_is_exact_when_the_tiers_agree(tmp_path: Path):
    idx, dt = _write(tmp_path)
    fixed = pd.read_parquet(dt)
    fixed = fixed[fixed["path"] != "w"]
    fixed.loc[fixed["path"] == "x", ["size", "sum_storage_class_id_2"]] = 30
    fixed.loc[fixed["path"] == "x", "n_files"] = 3
    fixed = pd.concat([fixed, pd.DataFrame([{"path": "z", "usr": None, "size": 0, "n_files": 0, "mtime_mean": None, "sum_storage_class_id_2": 0}])])
    fixed.to_parquet(dt)
    r = compare("b", idx, dt)
    assert r["ok"] is True
    assert render(r).splitlines()[-1] == "  OK: exact"
