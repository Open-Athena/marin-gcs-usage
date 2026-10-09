"""Publication recovery must prove completion without repeating data writes."""

import json
import uuid
from types import SimpleNamespace

import pytest

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import narrow
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.serve import Store

from chserver import ch_db, ch_url  # noqa: F401
from test_box import write_v2
from test_chstore import A, B


@pytest.mark.parametrize("records", [[], [("wrong SQL", 3, 100)], [("INSERT x", 3, 100)] * 2])
def test_recovery_requires_unique_exact_completion(records: list) -> None:
    ch = SimpleNamespace(db="source", json=lambda sql: [
        ["narrow:target:history_nodes_0", *record] for record in records
    ])
    recovery = narrow.HistoryPublicationRecovery(ch, "target")
    with pytest.raises(ValueError) as caught:
        recovery.verify("history_nodes_0", "INSERT x")
    assert str(caught.value) == "history publication recovery needs one completed matching query: history_nodes_0"
    assert recovery.written == {}


@pytest.mark.parametrize("actual", [4, 5])
def test_recovery_sums_batches_and_checks_tables(actual: int) -> None:
    reads = []

    def scalar(sql: str) -> str:
        reads.append(sql)
        return str(actual)

    ch = SimpleNamespace(db="source", scalar=scalar, json=lambda sql: [
        ["narrow:target:history_nodes_empty", "CREATE x", 0, 10],
        ["narrow:target:history_nodes_0", "INSERT x0", 2, 100],
        ["narrow:target:history_nodes_2", "INSERT x2", 3, 200],
    ])
    recovery = narrow.HistoryPublicationRecovery(ch, "target")
    assert [recovery.verify(label, query) for label, query in [
        ("history_nodes_empty", "CREATE x"), ("history_nodes_0", "INSERT x0"), ("history_nodes_2", "INSERT x2"),
    ]] == [.01, .1, .2]
    assert recovery.written == {"nodes_history": 5}
    if actual == 4:
        with pytest.raises(ValueError) as caught:
            recovery.check_counts()
        assert str(caught.value) == "history publication recovery row count differs: nodes_history: 4 != 5"
    else:
        recovery.check_counts()
    assert reads == ["SELECT count() FROM target.nodes_history"]


@pytest.mark.parametrize("coalescer", ["pair", "window"])
def test_completed_history_publication_recovery(ch_url, ch_db, tmp_path, monkeypatch, coalescer):  # noqa: F811
    target = f"recovery_test_{uuid.uuid4().hex[:10]}"
    source = f"{target}_source"
    admin = Ch(ch_url, session=False)
    admin.exec(f"CREATE DATABASE {source}")
    ch = Ch(ch_url, db=source)
    for date, files in [("2026-09-30", A), ("2026-10-01", B)]:
        ci.Ingest(ch, date, write_v2(tmp_path / date, files)[0], threads=2, log=lambda *args: None).run()
    store = Store(ch_url, db=source, threads=2)
    reserve = narrow.disk_reserve
    options = dict(batch_rows=2, numeric_parents=True, numeric_ancestors=True, coalescer=coalescer, log=lambda *args: None)

    def fail_publication(client: Ch, label: str, minimum: int) -> None:
        if label == f"create_{target}_h0":
            raise ValueError("injected publication reserve failure")
        reserve(client, label, minimum)

    try:
        narrow.build(store, target, "b/u1", ("2026-09-30", "2026-10-01"), union_engine="merge", log=lambda *args: None)
        monkeypatch.setattr(narrow, "disk_reserve", fail_publication)
        with pytest.raises(ValueError) as caught:
            narrow.history(store, target, **options)
        assert str(caught.value) == "injected publication reserve failure"
        assert ch.scalar(f"EXISTS DATABASE {target}_h0") == "0"
        tables = ["parent_ids", "parent_paths", "hierarchy", "numeric_parents", "nodes_history", "metadata_history", "nodes_history_by_name", "metadata_history_by_parent"]
        before = {table: ch.json(f"SELECT * FROM {target}.{table} ORDER BY tuple(*)") for table in tables}
        ch.exec("SYSTEM FLUSH LOGS")
        monkeypatch.setattr(narrow, "disk_reserve", reserve)
        # A different batch plan cannot accidentally adopt the original build.
        with pytest.raises(ValueError) as caught:
            narrow.history(store, target, **{**options, "batch_rows": 3}, resume_publication=True)
        assert str(caught.value) == "history publication recovery needs one completed matching query: history_nodes_0"
        assert ch.scalar(f"EXISTS DATABASE {target}_h0") == "0"
        result = narrow.history(store, target, **options, resume_publication=True)
        assert result["publication_recovered"] is True
        assert result["history_coalescer"] == coalescer
        assert json.loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest")) == result
        assert {table: ch.json(f"SELECT * FROM {target}.{table} ORDER BY tuple(*)") for table in tables} == before
        for index in range(2):
            for table in ("nodes", "metadata"):
                cols = "pre, post, depth, nid, b, o, path" if table == "nodes" else f"pre, depth, path, parent, {narrow.AGG_COLS}"
                assert ch.json(f"SELECT {cols} FROM {target}_h{index}.{table} ORDER BY pre") == ch.json(f"SELECT {cols} FROM {target}_{index}.{table} ORDER BY pre")
    finally:
        for database in [target, f"{target}_0", f"{target}_1", f"{target}_h0", f"{target}_h1", source]:
            admin.exec(f"DROP DATABASE IF EXISTS {database} SYNC")
        ch.close()
        admin.close()
