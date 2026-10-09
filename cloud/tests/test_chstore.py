"""The ClickHouse store (`dt_cloud.chstore`, specs/ch-store.md): ingest's
intervals reproduce every scan exactly, re-runs and interrupted runs are
idempotent, and partial scans are refused."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore.client import Ch, ChError
from dt_cloud.chstore.schema import dt_lit, live
from dt_cloud.chstore.serve import Store
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import OWN, write_v2

A = [f for f in OWN if not f[0].startswith("c/many")]
B = [f for f in A if f[0] != "b/u1/ckpt/b.bin"] + [("b/u1/ckpt/z.bin", "alice", 70, 20009, None, None), ("b/u3/ckpt/n.bin", None, 5, 20009, None, None)]
B = [(p, u, 260, mt, lr, c) if p == "b/u2/ckpt/c.bin" else (p, u, s, mt, lr, c) for p, u, s, mt, lr, c in B]
C = [f for f in B if not f[0].startswith("b/u3/")]
DAYS = {"2026-09-29": A, "2026-09-30": B, "2026-10-01": C}


@pytest.mark.parametrize("records,expected", [
    ([], []),
    ([["first", "2026-09-28 00:00:00", 1, 10, 10]], [("first", "2026-09-28 00:00:00", 1, 10, "2026-09-28 00:00:00")]),
    ([
        ["first", "2026-09-28 00:00:00", 1, 10, 10],
        ["switch", "2026-09-29 00:00:00", 2, 12, 5],
        ["reset", "2026-09-30 00:00:00", 2, 12, 12],
        ["churn", "2026-10-01 00:00:00", 2, 13, 2],
    ], [
        ("first", "2026-09-28 00:00:00", 1, 10, "2026-09-28 00:00:00"),
        ("switch", "2026-09-29 00:00:00", 2, 12, "2026-09-28 00:00:00"),
        ("reset", "2026-09-30 00:00:00", 2, 12, "2026-09-30 00:00:00"),
        ("churn", "2026-10-01 00:00:00", 2, 13, "2026-09-30 00:00:00"),
    ]),
])
def test_ingest_and_serving_share_proven_scan_epochs(
    monkeypatch: pytest.MonkeyPatch,
    records: list[list],
    expected: list[tuple],
) -> None:
    from dt_cloud.chstore import schema, serve

    calls = []
    ch = SimpleNamespace(json=lambda sql: calls.append(sql) or records)
    assert schema.scan_epochs(ch) == expected
    previous = ci.Ingest(ch, "2026-10-05", "source.parquet").prev()
    if expected:
        ident, dt, version, rows, epoch = expected[-1]
        assert previous == ci.Prev(dt, ident, version, rows, epoch)
    else:
        assert previous is None
    monkeypatch.setattr(serve, "Ch", lambda *a, **kw: ch)
    assert list(serve.Store("http://example").scans().values()) == [
        serve.Scan(ident, dt, version, epoch) for ident, dt, version, _, epoch in expected
    ]
    assert calls == ["SELECT id, toString(scan), version, rows, opened FROM scans FINAL ORDER BY scan"] * 3


def test_previous_ingest_scan_bounds_both_versions_and_closures():
    previous = ci.Prev("2026-10-04 00:00:00", "2026-10-04", 2, 12, "2026-09-30 00:00:00")
    assert previous.live("depth = 2") == (
        "(vf >= toDateTime('2026-09-30 00:00:00', 'UTC') AND vf <= toDateTime('2026-10-04 00:00:00', 'UTC') "
        "AND (depth, path, usr, vf) NOT IN (SELECT depth, path, usr, vf FROM closures "
        "WHERE vf >= toDateTime('2026-09-30 00:00:00', 'UTC') AND vt <= toDateTime('2026-10-04 00:00:00', 'UTC') AND (depth = 2)))"
    )


def test_ingest_guard_and_diff_use_previous_scan_epoch(monkeypatch):
    queries = []
    previous = ci.Prev("2026-10-04 00:00:00", "2026-10-04", 2, 12, "2026-09-30 00:00:00")
    conditions = []
    def previous_live(self, restrict="1"):
        conditions.append((self, restrict))
        return "proven_previous_live"

    monkeypatch.setattr(ci.Prev, "live", previous_live)
    ch = SimpleNamespace(rows=lambda sql: queries.append(sql) or [], exec=lambda *a, **kw: None,
                         fork=lambda: SimpleNamespace(exec=lambda *a, **kw: None))
    ing = ci.Ingest(ch, "2026-10-05", "source.parquet", server_file=True, threads=2, pairs=1, log=lambda *a: None)
    ing.version = 2
    monkeypatch.setattr(ing, "_ranges", lambda *a: ["depth = 2"])
    ing._guard(12, previous)
    ing._diff(previous)
    assert conditions == [(previous, "depth = 1"), (previous, "depth = 2")]
    assert queries == [
        "SELECT DISTINCT path FROM nodes WHERE depth = 1 AND proven_previous_live\n"
        "            AND path NOT IN (SELECT path FROM file('source.parquet', Parquet) WHERE depth = 1) ORDER BY path",
    ]


@pytest.mark.parametrize("version,published,actual,error", [
    (1, True, [[1, "b", "", "dir", 100, 2, 1, 2, 20, 15, 100, -1, 0, 0, 0]], None),
    (2, True, [[1, "b", "", "dir", 100, 2, 1, 2, 20, 15, 100, -1, 0, 0, 0]], None),
    (2, True, [[1, "b", "", "dir", 101, 2, 1, 2, 20, 15, 100, -1, 0, 0, 0]], "scan 2026-10-05: bucket/owner root slices differ from source.parquet"),
    (2, False, [], "scan 2026-10-05 is not published"),
])
def test_root_audit_is_read_only(version, published, actual, error):
    calls = []
    expected = [[1, "b", "", "dir", 100, 2, 1, 2, 20, 15, 100, -1, 0, 0, 0]]

    def scalar(sql):
        calls.append(sql)
        return str(version) if published else None

    def rows(sql):
        calls.append(sql)
        return expected if len(calls) == 2 else actual

    ch = SimpleNamespace(scalar=scalar, json=rows)
    if error:
        with pytest.raises(ci.IngestError) as caught:
            ci.audit_roots(ch, "2026-10-05", "source.parquet")
        assert str(caught.value) == error
    else:
        assert ci.audit_roots(ch, "2026-10-05", "source.parquet") == {
            "scan": "2026-10-05", "version": version, "buckets": 1, "owner_slices": 1,
            "bytes": 100, "objects": 2, "root_values_exact": True,
            "mtime_mean_comparison": "rounded seconds",
        }
    assert calls[0] == "SELECT version FROM scans FINAL WHERE scan = toDateTime('2026-10-05 00:00:00', 'UTC')"
    assert len(calls) == (3 if published else 1)
    if published:
        columns = "depth, path, usr, toString(kind), size, n_files, n_children, n_desc, mtime, round(mtime_mean), mtime_w, last_read, c2, c3, c4"
        source_columns = "depth, path, usr, toString(kind1), size1, n_files1, n_children1, n_desc1, mtime1, round(mtime_mean1), mtime_w1, last_read1, c21, c31, c41"
        merged = ", ".join(f"{ci.MERGED[c]} AS {c}1" for c in ci.VALUE_COLS)
        source = ci.V2_SELECT if version == 2 else ci.V1_SELECT
        assert calls[1:] == [
            f"SELECT {source_columns} FROM (\n"
            f"        SELECT depth, path, usr, {merged} FROM (\n"
            f"            SELECT {source} FROM file('source.parquet', Parquet) WHERE depth = 1\n"
            "        ) GROUP BY depth, path, usr\n"
            "    ) ORDER BY depth, path, usr",
            f"SELECT {columns} FROM nodes WHERE depth = 1 AND "
            "(vf <= toDateTime('2026-10-05 00:00:00', 'UTC') AND (depth, path, usr, vf) NOT IN "
            "(SELECT depth, path, usr, vf FROM closures WHERE vt <= toDateTime('2026-10-05 00:00:00', 'UTC') AND (depth = 1))) "
            "ORDER BY depth, path, usr",
        ]


def test_root_audit_cli(monkeypatch):
    calls = []

    def audit(ch, scan_id, source):
        calls.append((ch.db, scan_id, source, ch.url))
        return {"root_values_exact": True}

    monkeypatch.setattr(ci, "audit_roots", audit)
    result = CliRunner().invoke(main, ["ch-ingest-root-audit", "-B", "audit_test", "-d", "2026-10-05", "-U", "http://example", "source.parquet"])
    assert result.exit_code == 0, result.output
    assert result.output == '{"root_values_exact": true}\n'
    assert calls == [("audit_test", "2026-10-05", "source.parquet", "http://example")]


@pytest.mark.parametrize("pairs", [None, 1])
def test_ingest_range_has_explicit_memory_and_spill_limits(monkeypatch, pairs):
    settings = []
    ch = SimpleNamespace(exec=lambda *a, **kw: None,
                         fork=lambda: SimpleNamespace(exec=lambda *a, **kw: settings.append(kw["settings"])))
    ing = ci.Ingest(ch, "2026-10-05", "source.parquet", threads=8, pairs=pairs)
    monkeypatch.setenv("CH_INGEST_PAIRS", "1" if pairs is None else "3")
    monkeypatch.setattr(ing, "_ranges", lambda *a: ["1"])
    ing._diff(None)
    assert ing.t["pairs"] == 1
    assert settings == [{
        "max_threads": 8, "max_insert_threads": 1,
        "max_memory_usage": 8 << 30,
        "max_bytes_before_external_group_by": 256 << 20,
        "max_bytes_ratio_before_external_group_by": 0,
        "max_bytes_before_external_sort": 256 << 20,
        "max_bytes_ratio_before_external_sort": 0,
        "max_bytes_before_external_join": 1 << 30,
        "max_bytes_ratio_before_external_join": 0,
        "join_algorithm": "grace_hash", "grace_hash_join_initial_buckets": 16,
        "join_use_nulls": 0, "input_format_parquet_filter_push_down": 1,
    }]


def test_ingest_pairs_cli(monkeypatch):
    calls = []

    def ingest(ch, scan, src, **kwargs):
        calls.append((scan, src, kwargs))
        return SimpleNamespace(run=lambda: {"scan": scan})

    monkeypatch.setattr(ci, "Ingest", ingest)
    monkeypatch.setattr(ci, "sizes", lambda ch: {})
    result = CliRunner().invoke(main, ["ch-ingest", "-d", "2026-10-05", "-F", "-j", "1", "-t", "8", "2026-10-05.parquet"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"scan": "2026-10-05", "sizes": {}}
    assert calls == [("2026-10-05", "2026-10-05.parquet", {"server_file": True, "force": False, "allow_drop": False, "threads": 8, "pairs": 1, "stage_dir": None})]


@pytest.mark.parametrize("pairs", [0, -1])
def test_ingest_refuses_invalid_pairs(pairs):
    with pytest.raises(ci.IngestError) as caught:
        ci.Ingest(None, "2026-10-05", "source.parquet", pairs=pairs)
    assert str(caught.value) == "ingest pairs must be positive"


@pytest.mark.parametrize("pairs", [1, 2])
def test_ingest_does_not_schedule_more_ranges_after_a_failure(monkeypatch, pairs):
    calls, submitted = [], []

    class RecordingPool(ThreadPoolExecutor):
        def submit(self, fn, cond):
            submitted.append(cond)
            return super().submit(fn, cond)

    def execute(sql, **kwargs):
        condition = re.search(r"FROM file\('source.parquet', Parquet\) WHERE (.*?)\) GROUP BY", sql).group(1)
        calls.append(condition)
        if condition == "depth < 2":
            raise ci.IngestError("range failed")

    ch = SimpleNamespace(exec=lambda *a, **kw: None, fork=lambda: SimpleNamespace(exec=execute))
    ing = ci.Ingest(ch, "2026-10-05", "source.parquet", server_file=True, threads=8, pairs=pairs)
    monkeypatch.setattr(ci, "ThreadPoolExecutor", RecordingPool)
    monkeypatch.setattr(ing, "_ranges", lambda *a: ["depth < 2", "depth = 2", "depth > 2"])
    with pytest.raises(ci.IngestError) as caught:
        ing._diff(None)
    assert str(caught.value) == "range failed"
    expected = ["depth < 2", "depth = 2"][:pairs]
    assert submitted == expected
    assert sorted(calls) == sorted(expected)


def test_ingest_logs_completed_ranges(monkeypatch):
    logs = []
    ch = SimpleNamespace(exec=lambda *a, **kw: None, fork=lambda: SimpleNamespace(exec=lambda *a, **kw: None))
    ing = ci.Ingest(ch, "2026-10-05", "source.parquet", threads=8, pairs=1, log=logs.append)
    monkeypatch.setattr(ing, "_ranges", lambda *a: ["depth < 2", "depth >= 2"])
    ing._diff(None)
    assert [re.sub(r"\(\d+\.\d+s\)", "(<duration>s)", line) for line in logs] == [
        "ch-ingest 2026-10-05: ranges 1/2 completed (<duration>s)",
        "ch-ingest 2026-10-05: ranges 2/2 completed (<duration>s)",
    ]


def src_rows(pf: str) -> list[tuple]:
    """A v2 parquet's rows in the store's encoding, sorted by key."""
    t = pq.read_table(pf).to_pylist()
    out = []
    for r in t:
        mean = r.get("mtime_mean")
        out.append((r["depth"], r["path"], r.get("usr") or "", r["kind"], r["size"], r["n_files"], r["n_children"] if r.get("n_children") is not None else -1,
                    -1 if r.get("n_desc") is None else r["n_desc"], -1 if r.get("mtime") is None else r["mtime"], 0.0 if mean is None else mean, 0 if mean is None else r["size"], -1 if r.get("last_read") is None else r["last_read"],
                    r.get("sum_storage_class_id_2") or 0, r.get("sum_storage_class_id_3") or 0, r.get("sum_storage_class_id_4") or 0))
    return sorted(out, key=lambda x: (x[0], x[1].encode(), x[2]))


def asof_rows(ch: Ch, d: str) -> list[tuple]:
    D = dt_lit(f"{d} 00:00:00")
    rows = ch.json(f"""SELECT depth, path, usr, toString(kind), size, n_files, n_children, n_desc, mtime, mtime_mean, mtime_w, last_read, c2, c3, c4
        FROM nodes WHERE depth > 0 AND {live(D)} ORDER BY depth, path, usr""")
    return [tuple(r) for r in rows]


@pytest.fixture(scope="module")
def store(ch_url, ch_db, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("days")
    files = {day: write_v2(d / day, fs)[0] for day, fs in DAYS.items()}
    ch = Ch(ch_url, db=ch_db)
    recs = [ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None).run() for day in DAYS]
    return {"ch": ch, "files": files, "recs": recs}


def test_temporary_tables_are_explicitly_dropped(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db)
    ch.tmp("tmp_cleanup", "SELECT 'x' AS path", disk=True)
    assert ch.scalar("SELECT count() FROM tmp_cleanup") == "1"
    ch.close()
    with pytest.raises(ChError):
        ch.scalar("SELECT count() FROM tmp_cleanup")


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


@pytest.mark.parametrize("day", list(DAYS))
def test_root_audit_reproduces_source_owner_slices(store, day):
    ch, source = store["ch"], store["files"][day]
    ci.Ingest(ch, day, source)._stage()
    roots = [row for row in src_rows(source) if row[0] == 1]
    assert ci.audit_roots(ch, day, ci.STAGE_FILE) == {
        "scan": day, "version": 2, "buckets": len({row[1] for row in roots}), "owner_slices": len(roots),
        "bytes": sum(row[4] for row in roots), "objects": sum(row[5] for row in roots), "root_values_exact": True,
        "mtime_mean_comparison": "rounded seconds",
    }
    wrong_day = "2026-09-30" if day != "2026-09-30" else "2026-10-01"
    with pytest.raises(ci.IngestError) as caught:
        ci.audit_roots(ch, wrong_day, ci.STAGE_FILE)
    assert str(caught.value) == f"scan {wrong_day}: bucket/owner root slices differ from {ci.STAGE_FILE}"


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
    with pytest.raises(ci.IngestError, match=r"scan 2026-09-30T0100 predates the newest scan \(2026-10-01\): the store only appends"):
        ci.Ingest(ch, "2026-09-30T0100", store["files"]["2026-09-30"], threads=2, log=lambda *a: None).run()
    # A scan without bucket `c`: refused (nothing written), unless told to close it.
    pf, _ = write_v2(tmp_path / "part", [f for f in C if f[0].startswith("b/")])
    before = ch.json("SELECT count() FROM nodes")
    with pytest.raises(ci.IngestError, match=r"lacks 1 root\(s\) the store has \(c\)"):
        ci.Ingest(ch, "2026-10-02", pf, threads=2, log=lambda *a: None).run()
    assert ch.json("SELECT count() FROM nodes") == before
    assert ch.json("SELECT name FROM system.tables WHERE database = currentDatabase() AND name LIKE 'ingest%'") == []


def test_interrupted_runs_recover(ch_url, tmp_path):  # noqa: F811
    """A crash after the diff or names append: the re-run drops this scan's
    partitions and ends where an uninterrupted run would."""
    import uuid

    files = {day: write_v2(tmp_path / day, fs)[0] for day, fs in DAYS.items()}
    dbs = []
    for mode in ("clean", "after-diff", "after-names"):
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
                ing._stage()
                n_new = ing._source()
                ing._guard(n_new, ing.prev())
                ing._diff(ing.prev())
                if mode == "after-names":
                    ing._names(first=False)
            ing.run()
    snap = [[asof_rows(Ch(ch_url, db=db), day) for day in DAYS] + [Ch(ch_url, db=db).json("SELECT at, sign, path, usr, size FROM changes ORDER BY ALL")] for db in dbs]
    try:
        assert snap[1] == snap[0]
        assert snap[2] == snap[0]
    finally:
        for db in dbs:
            Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_history_is_append_only(store):
    ch = store["ch"]
    assert ch.json("SELECT toString(vf), count() FROM nodes GROUP BY vf ORDER BY vf") == [
        ["2026-09-29 00:00:00", 18],
        ["2026-09-30 00:00:00", 12],
        ["2026-10-01 00:00:00", 3],
    ]
    assert ch.json("SELECT toString(vt), count() FROM closures GROUP BY vt ORDER BY vt") == [
        ["2026-09-30 00:00:00", 9],
        ["2026-10-01 00:00:00", 6],
    ]


def test_read_only_ingest_plans_preserve_published_tables(store, monkeypatch):
    from dt_cloud.chstore import ingest_bench

    ch = store["ch"]
    source = store["files"]["2026-10-01"]
    ci.Ingest(ch, "2026-10-01", source)._stage()
    monkeypatch.setattr(ingest_bench, "sample_bounds", lambda ch, n: ["depth = 1", "depth > 1"])
    queries = [f"SELECT * FROM {table} ORDER BY {key}" for table, key in (
        ("nodes", "depth, path, usr, vf"), ("closures", "depth, path, usr, vf, vt"),
        ("changes", "at, depth, path, usr, sign, vf"), ("scans", "scan, id"),
    )]
    before = [ch.exec(query) for query in queries]
    rows = []
    ingest_bench.benchmark(ch, "2026-10-01", ci.STAGE_FILE, (0, 1), trials=1, threads=2, emit=rows.append)
    assert [(r["range"], r["plan"], r["same_rows_fingerprint"], r["publication"]) for r in rows] == [
        (0, "legacy", True, False), (0, "epoch", True, False),
        (0, "epoch-1g", True, False), (0, "epoch-merge", True, False),
        (1, "epoch-merge", True, False), (1, "epoch-1g", True, False),
        (1, "epoch", True, False), (1, "legacy", True, False),
    ]
    assert [ch.exec(query) for query in queries] == before


def test_query_epoch_requires_a_complete_rebaseline(ch_url):  # noqa: F811
    """A format switch can leave unchanged slices open. Prune older versions only when the ingest
    counts prove every live row opened at the new scan; ordinary churn inherits that lower bound."""
    import uuid

    from dt_cloud.chstore.schema import SCANS

    db = f"t_{uuid.uuid4().hex[:10]}"
    admin = Ch(ch_url, db="default", session=False)
    admin.exec(f"CREATE DATABASE {db}")
    try:
        ch = Ch(ch_url, db=db)
        ch.exec(SCANS)
        ch.exec("""INSERT INTO scans (scan, id, version, rows, opened, closed, s, src) VALUES
            ('2026-09-28', '2026-09-28', 1, 10, 10, 0, 0, ''),
            ('2026-09-29', '2026-09-29', 2, 12, 5, 3, 0, ''),
            ('2026-09-30', '2026-09-30', 2, 12, 12, 12, 0, ''),
            ('2026-10-01', '2026-10-01', 2, 13, 2, 1, 0, '')""")
        assert [(s.id, s.epoch) for s in Store(ch_url, db=db).scans().values()] == [
            ("2026-09-28", "2026-09-28 00:00:00"),
            ("2026-09-29", "2026-09-28 00:00:00"),
            ("2026-09-30", "2026-09-30 00:00:00"),
            ("2026-10-01", "2026-09-30 00:00:00"),
        ]
    finally:
        admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_ingest_preserves_old_slices_across_incomplete_format_switch(ch_url, tmp_path):
    import uuid

    import pyarrow as pa

    v1 = tmp_path / "v1-unchanged-root.parquet"
    pq.write_table(pa.table({
        "path": ["b"], "depth": [1], "usr": [None], "b": [10], "o": [1], "wts": [1000.0], "wb": [10],
        "c2": [0], "c3": [0], "c4": [0], "a": [None],
    }), v1)

    def source(
        day: str,
        total: int,
        files: dict[str, int],
    ) -> str:
        path = tmp_path / f"{day}.parquet"
        sizes = [total, *files.values()]
        rows = len(sizes)
        pq.write_table(pa.table({
            "path": ["b", *[f"b/{name}" for name in files]], "depth": [1, *[2] * len(files)], "usr": [None] * rows,
            "kind": ["dir", *["file"] * len(files)], "size": sizes, "n_files": [len(files), *[1] * len(files)],
            "n_children": [None] * rows, "n_desc": [None] * rows, "mtime": [None] * rows,
            "mtime_mean": [100.0] * rows, "last_read": [None] * rows,
            "sum_storage_class_id_2": [0] * rows, "sum_storage_class_id_3": [0] * rows, "sum_storage_class_id_4": [0] * rows,
        }), path)
        return str(path)

    inputs = [
        ("2026-09-28", str(v1)),
        ("2026-09-29", source("2026-09-29", 10, {"f": 10})),
        ("2026-09-30", source("2026-09-30", 20, {"f": 10, "g": 10})),
        ("2026-10-01", source("2026-10-01", 50, {"f": 20, "g": 30})),
        ("2026-10-02", source("2026-10-02", 55, {"f": 20, "g": 35})),
    ]
    db = f"t_{uuid.uuid4().hex[:10]}"
    admin = Ch(ch_url, db="default", session=False)
    admin.exec(f"CREATE DATABASE {db}")
    try:
        ch = Ch(ch_url, db=db)
        records = [ci.Ingest(ch, day, path, threads=2, log=lambda *a: None).run() for day, path in inputs]
        assert [(r["rows"], r["opened"], r["closed"]) for r in records] == [
            (1, 1, 0), (2, 1, 0), (3, 2, 1), (3, 3, 3), (3, 2, 2),
        ]
        assert [s.epoch for s in Store(ch_url, db=db).scans().values()] == [
            "2026-09-28 00:00:00", "2026-09-28 00:00:00", "2026-09-28 00:00:00",
            "2026-10-01 00:00:00", "2026-10-01 00:00:00",
        ]
        assert ci.Ingest(ch, "2026-10-03", "unused.parquet").prev() == ci.Prev(
            "2026-10-02 00:00:00", "2026-10-02", 2, 3, "2026-10-01 00:00:00",
        )
        for day, path in inputs[1:]:
            assert asof_rows(ch, day) == src_rows(path)
    finally:
        admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_v1_source_and_duplicate_slices(ch_url, tmp_path):  # noqa: F811
    """A v1 index (dirs only, the wire names) — whose writer could emit one
    path's unattributed slice twice — then a v2 scan of the same tree: the
    duplicates merge (as the Worker's `merge` sums them); at the switch every
    dir row gets a new version (v2 adds child counts) and the objects appear."""
    import uuid

    import pyarrow as pa

    v1 = tmp_path / "v1.parquet"
    pq.write_table(pa.table({
        "path": ["b", "b", "b", "b/u1", "b/u1"], "depth": [1, 1, 1, 2, 2], "usr": ["alice", None, None, "alice", None],
        "b": [10, 20, 5, 10, 25], "o": [1, 2, 1, 1, 3], "wts": [10.0 * 100, 20.0 * 200, 5.0 * 300, 1000.0, None], "wb": [10, 20, 5, 10, 0],
        "c2": [0, 0, 5, 0, 5], "c3": [0, 0, 0, 0, 0], "c4": [0, 0, 0, 0, 0], "a": [None, 7, 9, None, 9],
    }), v1)
    v2, _ = write_v2(tmp_path / "v2", A)
    db = f"t_{uuid.uuid4().hex[:10]}"
    Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
    try:
        ch = Ch(ch_url, db=db)
        r1 = ci.Ingest(ch, "2026-09-28", str(v1), threads=2, log=lambda *a: None).run()
        assert ci.audit_roots(ch, "2026-09-28", ci.STAGE_FILE) == {
            "scan": "2026-09-28", "version": 1, "buckets": 1, "owner_slices": 2,
            "bytes": 35, "objects": 4, "root_values_exact": True, "mtime_mean_comparison": "rounded seconds",
        }
        r2 = ci.Ingest(ch, "2026-09-29", v2, threads=2, log=lambda *a: None).run()
        assert [{k: r[k] for k in ("version", "rows", "opened", "closed")} for r in (r1, r2)] == [
            {"version": 1, "rows": 4, "opened": 4, "closed": 0},
            # Every v1 row closes (none of the 4 has a v2 twin with equal values), the 18 v2 rows open.
            {"version": 2, "rows": 18, "opened": 18, "closed": 4},
        ]
        assert asof_rows(ch, "2026-09-28") == [
            (1, "b", "", "dir", 25, 3, -1, -1, -1, (20.0 * 200 + 5.0 * 300) / 25, 25, 9, 5, 0, 0),
            (1, "b", "alice", "dir", 10, 1, -1, -1, -1, 100.0, 10, -1, 0, 0, 0),
            (2, "b/u1", "", "dir", 25, 3, -1, -1, -1, 0.0, 0, 9, 5, 0, 0),
            (2, "b/u1", "alice", "dir", 10, 1, -1, -1, -1, 100.0, 10, -1, 0, 0, 0),
        ]
        assert ch.json("SELECT version FROM scans ORDER BY scan") == [[1], [2]]
        scans = Store(ch_url, db=db).scans()
        assert [(s.id, s.dt, s.version, s.epoch) for s in scans.values()] == [
            ("2026-09-28", "2026-09-28 00:00:00", 1, "2026-09-28 00:00:00"),
            ("2026-09-29", "2026-09-29 00:00:00", 2, "2026-09-29 00:00:00"),
        ]
        assert scans["2026-09-29"].live() == (
            "(vf >= toDateTime('2026-09-29 00:00:00', 'UTC') AND vf <= toDateTime('2026-09-29 00:00:00', 'UTC') "
            "AND (depth, path, usr, vf) NOT IN (SELECT depth, path, usr, vf FROM closures WHERE "
            "vf >= toDateTime('2026-09-29 00:00:00', 'UTC') AND vt <= toDateTime('2026-09-29 00:00:00', 'UTC') AND (1)))"
        )
    finally:
        Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_pairing_in_key_ranges(ch_url, monkeypatch):  # noqa: F811
    """The diff cut into many key ranges gives exactly the scans: the
    `v2-search` generation, then `v2-search-b`."""
    import uuid

    from dt_cloud.chstore import schema
    from test_bench_truth import PATH_F

    monkeypatch.setattr(schema, "NODES", schema.NODES.replace(
        "SETTINGS deduplicate_merge_projection_mode",
        "SETTINGS index_granularity = 8, deduplicate_merge_projection_mode",
    ))
    path_c = PATH_F.replace("/v2-search/", "/v2-search-b/")
    db = f"t_{uuid.uuid4().hex[:10]}"
    Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
    try:
        ch = Ch(ch_url, db=db)
        recs = [ci.Ingest(ch, d, pf, threads=6, log=lambda *a: None).run() for d, pf in (("2026-10-01", PATH_F), ("2026-10-02", path_c))]
        # A precondition, not the spec: both comparisons were divided into many bounded joins.
        assert all(r["steps"]["ranges"] > 20 for r in recs)
        for d, pf in (("2026-10-01", PATH_F), ("2026-10-02", path_c)):
            got = asof_rows(ch, d)
            assert [g[:9] + (round(g[9], 6),) + g[10:] for g in got] == [w[:9] + (round(w[9], 6),) + w[10:] for w in src_rows(pf)], d
    finally:
        Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_mean_jitter_opens_no_version(ch_url, tmp_path):  # noqa: F811
    """A mean stamp that moves by under a second (float-sum jitter between scans) is the same version;
    a second or more is a new one."""
    import uuid

    import pyarrow as pa

    def v1(path, wts):
        pq.write_table(pa.table({"path": ["b", "c"], "depth": [1, 1], "usr": [None, None], "b": [10, 10], "o": [1, 1], "wts": wts, "wb": [10, 10],
                                 "c2": [0, 0], "c3": [0, 0], "c4": [0, 0], "a": [None, None]}), path)
        return str(path)

    db = f"t_{uuid.uuid4().hex[:10]}"
    Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
    try:
        ch = Ch(ch_url, db=db)
        days = [("2026-09-28", [1000.0 * 10, 1000.0 * 10]), ("2026-09-29", [1000.0 * 10 + 1e-6, 1000.0 * 10]), ("2026-09-30", [1000.0 * 10, 1002.0 * 10])]
        recs = [ci.Ingest(ch, d, v1(tmp_path / f"{d}.parquet", w), threads=2, log=lambda *a: None).run() for d, w in days]
        assert [(r["opened"], r["closed"]) for r in recs] == [(2, 0), (0, 0), (1, 1)]
        for day, _ in days:
            ci.Ingest(ch, day, str(tmp_path / f"{day}.parquet"))._stage()
            assert ci.audit_roots(ch, day, ci.STAGE_FILE) == {
                "scan": day, "version": 1, "buckets": 2, "owner_slices": 2,
                "bytes": 20, "objects": 2, "root_values_exact": True, "mtime_mean_comparison": "rounded seconds",
            }
    finally:
        Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")
