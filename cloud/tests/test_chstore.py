"""The ClickHouse store (`dt_cloud.chstore`, specs/ch-store.md): ingest's
intervals reproduce every scan exactly, re-runs and interrupted runs are
idempotent, and partial scans are refused."""

import pyarrow.parquet as pq
import pytest

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.schema import OPEN

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import OWN, write_v2

A = [f for f in OWN if not f[0].startswith("c/many")]
B = [f for f in A if f[0] != "b/u1/ckpt/b.bin"] + [("b/u1/ckpt/z.bin", "alice", 70, 20009, None, None), ("b/u3/ckpt/n.bin", None, 5, 20009, None, None)]
B = [(p, u, 260, mt, lr, c) if p == "b/u2/ckpt/c.bin" else (p, u, s, mt, lr, c) for p, u, s, mt, lr, c in B]
C = [f for f in B if not f[0].startswith("b/u3/")]
DAYS = {"2026-09-29": A, "2026-09-30": B, "2026-10-01": C}


def src_rows(pf: str) -> list[tuple]:
    """A v2 parquet's rows in the store's encoding, sorted by key."""
    t = pq.read_table(pf).to_pylist()
    out = []
    for r in t:
        mean = r.get("mtime_mean")
        out.append((r["depth"], r["path"], r["usr"] or "", r["kind"], r["size"], r["n_files"], r["n_children"] if r.get("n_children") is not None else -1,
                    -1, -1, 0.0 if mean is None else mean, 0 if mean is None else r["size"], -1 if r.get("last_read") is None else r["last_read"],
                    r["sum_storage_class_id_2"], r["sum_storage_class_id_3"], r["sum_storage_class_id_4"]))
    return sorted(out, key=lambda x: (x[0], x[1].encode(), x[2]))


def asof_rows(ch: Ch, d: str) -> list[tuple]:
    rows = ch.json(f"""SELECT depth, path, usr, toString(kind), size, n_files, n_children, n_desc, mtime, mtime_mean, mtime_w, last_read, c2, c3, c4
        FROM nodes WHERE depth > 0 AND vf <= toDateTime('{d} 00:00:00', 'UTC') AND vt > toDateTime('{d} 00:00:00', 'UTC') ORDER BY depth, path, usr""")
    return [tuple(r) for r in rows]


@pytest.fixture(scope="module")
def store(ch_url, ch_db, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("days")
    files = {day: write_v2(d / day, fs)[0] for day, fs in DAYS.items()}
    ch = Ch(ch_url, db=ch_db)
    recs = [ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None).run() for day in DAYS]
    return {"ch": ch, "files": files, "recs": recs}


def test_ingest_counts(store):
    got = [{k: r[k] for k in ("scan", "version", "rows", "opened", "closed")} for r in store["recs"]]
    # 09-29: 18 (path, owner) slices. 09-30: `b.bin` gone, `z.bin` and the 3 `u3` rows new, `c.bin` resized; every
    # ancestor of a change gets a version (`b`'s 3 slices, `u1`, `u1/ckpt`, `u2`'s bob slice, `u2/ckpt`): 12 opened,
    # 9 closed. 10-01: `u3` gone again, `b`'s 3 slices back to two children: 3 opened, 6 closed.
    assert got == [
        {"scan": "2026-09-29", "version": 2, "rows": 18, "opened": 18, "closed": 0},
        {"scan": "2026-09-30", "version": 2, "rows": 21, "opened": 12, "closed": 9},
        {"scan": "2026-10-01", "version": 2, "rows": 18, "opened": 3, "closed": 6},
    ]


def test_asof_reproduces_every_scan(store):
    ch = store["ch"]
    for day, pf in store["files"].items():
        want = src_rows(pf)
        got = asof_rows(ch, day)
        assert [g[:9] + (round(g[9], 6),) + g[10:] for g in got] == [w[:9] + (round(w[9], 6),) + w[10:] for w in want], day
        # Between scans (a day with no scan), the earlier scan answers.
    assert asof_rows(ch, "2026-10-02") == asof_rows(ch, "2026-10-01")
    assert asof_rows(ch, "2026-09-28") == []


def test_rerun_is_a_nop(store):
    ch = store["ch"]
    before = ch.json("SELECT count(), sum(size) FROM nodes")
    assert ci.Ingest(ch, "2026-10-01", store["files"]["2026-10-01"], threads=2, log=lambda *a: None).run() == {"scan": "2026-10-01", "nop": "already ingested"}
    assert ch.json("SELECT count(), sum(size) FROM nodes") == before


def test_refuses_older_and_partial(store, tmp_path):
    ch = store["ch"]
    with pytest.raises(ci.IngestError, match="predates the open versions"):
        ci.Ingest(ch, "2026-09-30T0100", store["files"]["2026-09-30"], threads=2, log=lambda *a: None).run()
    # A scan without bucket `c`: refused (nothing written), unless told to close it.
    pf, _ = write_v2(tmp_path / "part", [f for f in C if f[0].startswith("b/")])
    before = ch.json("SELECT count() FROM nodes")
    with pytest.raises(ci.IngestError, match=r"lacks 1 root\(s\) the store has \(c\)"):
        ci.Ingest(ch, "2026-10-02", pf, threads=2, log=lambda *a: None).run()
    assert ch.json("SELECT count() FROM nodes") == before
    assert ch.json("SELECT name FROM system.tables WHERE database = currentDatabase() AND name LIKE 'ingest%'") == []


def test_interrupted_runs_recover(ch_url, tmp_path):  # noqa: F811
    """A crash after the closed rows were appended, or after the swap: the
    re-run ends where an uninterrupted run would."""
    import uuid

    files = {day: write_v2(tmp_path / day, fs)[0] for day, fs in DAYS.items()}
    dbs = []
    for mode in ("clean", "after-pair", "after-swap"):
        db = f"t_{uuid.uuid4().hex[:10]}"
        Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
        dbs.append(db)
        ch = Ch(ch_url, db=db)
        for day in DAYS:
            ing = ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None)
            if day == "2026-09-30" and mode != "clean":
                # Run the steps by hand, then stop.
                from dt_cloud.chstore.schema import create

                create(ch)
                ing._cleanup()
                ing._load()
                ing._stage_open(ing.open_asof())
                ing._pair()
                if mode == "after-swap":
                    ing._names(first=False)
                    ch.exec(f"ALTER TABLE nodes REPLACE PARTITION ID '210601' FROM {ing.t_open}")
            ing.run()
    snap = [[asof_rows(Ch(ch_url, db=db), day) for day in DAYS] + [Ch(ch_url, db=db).json("SELECT at, sign, path, usr, size FROM changes ORDER BY ALL")] for db in dbs]
    try:
        assert snap[1] == snap[0]
        assert snap[2] == snap[0]
    finally:
        for db in dbs:
            Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_open_sentinel(store):
    ch = store["ch"]
    assert ch.json(f"SELECT toString(vf), path FROM nodes WHERE depth = 0") == [["2026-10-01 00:00:00", ""]]
    assert ch.json(f"SELECT count() FROM nodes WHERE vt = toDateTime('{OPEN}', 'UTC') AND depth > 0") == [[18]]
