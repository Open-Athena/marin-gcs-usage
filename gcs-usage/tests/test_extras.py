import json
from pathlib import Path

import duckdb
import pandas as pd

from gcs_usage.extras import ATTR_FILE, CK_FILE, ckpt_dirs, write_extras

DIRS = [
    "b", "b/grug",
    "b/grug/run1", "b/grug/run1/checkpoints", "b/grug/run1/checkpoints/step-100", "b/grug/run1/checkpoints/step-200", "b/grug/run1/hf",
    "b/grug/run2", "b/grug/run2/step-1", "b/grug/run2/step-2", "b/grug/run2/eval",
    "b/grug/run3", "b/grug/run3/step-1", "b/grug/run3/logs",  # one step child: not checkpoint-shaped
    "b/data", "b/data/x",
    "b/ckpts", "b/ckpts/a",
]


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE d(fp VARCHAR)")
    con.executemany("INSERT INTO d VALUES (?)", [(d,) for d in DIRS])
    return con


def test_ckpt_dirs_rule():
    # Own name (`b/ckpts`), a `checkpoints` child (`b`, `run1`), ≥ 2 step children (`run1/checkpoints`, `run2`).
    assert ckpt_dirs(_con(), "d") == ["b", "b/ckpts", "b/grug/run1", "b/grug/run1/checkpoints", "b/grug/run2"]


def test_write_extras(tmp_path: Path):
    pfx = pd.DataFrame([
        {"key": "b/grug", "user": "calvin", "source": "wandb-run"},
        {"key": "b/data", "user": "ryan", "source": "manual"},
    ])
    assert write_extras(_con(), "d", pfx, tmp_path) == {"ck": 5, "attr": 2}
    assert json.loads((tmp_path / CK_FILE).read_text()) == {"v": 1, "ck": ["b", "b/ckpts", "b/grug/run1", "b/grug/run1/checkpoints", "b/grug/run2"]}
    assert json.loads((tmp_path / ATTR_FILE).read_text()) == {"v": 1, "attr": {"b/grug": ["calvin", "wandb-run", None], "b/data": ["ryan", "manual", None]}}


def test_write_extras_without_attribution(tmp_path: Path):
    assert write_extras(_con(), "d", None, tmp_path) == {"ck": 5}
    assert sorted(p.name for p in tmp_path.iterdir()) == [CK_FILE]
