"""Index extras (specs/index-extras.md): two JSON sidecars beside a scan's
index tiers, written by ``webdata`` for a fresh scan and by ``gcs-usage
index-extras`` as the backfill for archived generations.

- ``ck.json``:   the checkpoint-shaped directories — computed over each dir's
                 FULL child list, which the site's pixel-budgeted subtree never
                 has — so "keep last ckpt" is offered exactly where it applies.
- ``attr.json``: every attributing prefix → ``[user, source, evidence]`` —
                 the provenance of inferred ownership, which the path index
                 drops on the way to its ``usr`` column.

Keys are index paths: ``<bucket>/<dir>/…`` (no ``gs://``, no trailing slash).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from utz import err

if TYPE_CHECKING:
    import duckdb
    import pandas as pd

# Mirrors the site's `looksCkpt` / `CKPT_SEG_RE` (site/src/sweep.ts): the
# dir's own name, a direct child's name, or ≥ 2 step-numbered children.
CKPT_NAME_RE = r"(^|[-_.])(ckpts?|checkpoints?)([-_.]|$)"
CKPT_SEG_RE = r"^(step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?\d+"

CK_FILE = "ck.json"
ATTR_FILE = "attr.json"


def ckpt_dirs(con: "duckdb.DuckDBPyConnection", dirs_sql: str) -> list[str]:
    """Checkpoint-shaped dirs from a relation with an ``fp`` column (one row
    per dir, ``bucket/a/b``; duplicates are fine)."""
    rows = con.execute(
        f"""
        WITH d AS (SELECT DISTINCT fp FROM {dirs_sql} WHERE fp IS NOT NULL AND fp <> ''),
        named AS (
          SELECT fp,
            CASE WHEN position('/' IN fp) > 0 THEN regexp_replace(fp, '/[^/]*$', '') END AS parent,
            regexp_extract(fp, '[^/]*$') AS name
          FROM d
        ),
        own AS (SELECT fp FROM named WHERE regexp_matches(name, ?, 'i')),
        kids AS (
          SELECT parent AS fp FROM named WHERE parent IS NOT NULL
          GROUP BY parent
          HAVING bool_or(regexp_matches(name, ?, 'i')) OR count_if(regexp_matches(name, ?, 'i')) >= 2
        )
        SELECT DISTINCT fp FROM (SELECT fp FROM own UNION ALL SELECT fp FROM kids) ORDER BY fp
        """,
        [CKPT_NAME_RE, CKPT_NAME_RE, CKPT_SEG_RE],
    ).fetchall()
    return [fp for (fp,) in rows]


def attr_map(pfx_df: "pd.DataFrame") -> dict[str, list]:
    """``key → [user, source, evidence]`` from the prefix-label frame
    (``prefix_labels``; ``evidence`` when the frame carries it)."""
    has_ev = "evidence" in pfx_df.columns
    out: dict[str, list] = {}
    for row in pfx_df.itertuples(index=False):
        ev = getattr(row, "evidence", None) if has_ev else None
        out[row.key] = [row.user, getattr(row, "source", None), None if ev is None or ev != ev else ev]
    return out


def write_extras(
    con: "duckdb.DuckDBPyConnection",
    dirs_sql: str,
    pfx_df: "pd.DataFrame | None",
    out_dir: Path,
) -> dict[str, int]:
    """Write ``ck.json`` (and ``attr.json`` when a prefix frame is given) into
    ``out_dir``; returns entry counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ck = ckpt_dirs(con, dirs_sql)
    (out_dir / CK_FILE).write_text(json.dumps({"v": 1, "ck": ck}, separators=(",", ":")) + "\n")
    counts = {"ck": len(ck)}
    if pfx_df is not None:
        attr = attr_map(pfx_df)
        (out_dir / ATTR_FILE).write_text(json.dumps({"v": 1, "attr": attr}, separators=(",", ":")) + "\n")
        counts["attr"] = len(attr)
    err(f"extras: {out_dir}: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    return counts
