"""Long canonical oracles get explicit limits, not changed live serving limits."""

import json
import re
import uuid
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.bench import queryset
from dt_cloud.bench.queryset import Case
from dt_cloud.chstore import narrow_serve
from dt_cloud.chstore.client import Ch, ChError, statement_timeout_settings
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401


@pytest.mark.parametrize("timeout", [None, 900])
def test_reference_budget_only_changes_explicit_oracle(monkeypatch, timeout):
    calls = []
    client = SimpleNamespace(settings={"max_threads": "8"}, timeout=300, close=lambda: calls.append("close"))
    store = SimpleNamespace(session=lambda: client, scan=lambda date: None, root_label="root")
    monkeypatch.setattr(narrow_serve.cs, "filter_prepare", lambda *args: None)
    monkeypatch.setattr(narrow_serve.cs, "subtree_body", lambda *args, **kwargs: ['{"ok": true}'])
    result = narrow_serve.compare_response(store, "2026-10-05", "", "needle", reference_timeout=timeout)
    assert {key: value for key, value in result.items() if key != "response_s"} == {"body": {"ok": True}, "bytes": 12}
    assert client.timeout == (960 if timeout is not None else 300)
    assert client.settings == {"max_threads": "8", **(statement_timeout_settings(900) if timeout is not None else {})}
    assert calls == ["close"]


@pytest.mark.parametrize("timeout", [0, -1])
def test_reference_budget_rejected_before_connection(timeout: int) -> None:
    with pytest.raises(ValueError) as caught:
        narrow_serve.compare_response(None, "date", "", "needle", reference_timeout=timeout)
    assert str(caught.value) == "statement timeout must be positive"


def test_statement_limit_throws_instead_of_returning_partial_results() -> None:
    assert statement_timeout_settings(900) == {
        "max_execution_time": "900", "timeout_before_checking_execution_speed": "0", "timeout_overflow_mode": "throw",
    }


@pytest.mark.parametrize("first", [False, True])
def test_reference_budget_cli_forwarding(monkeypatch, tmp_path, first):
    calls = []
    manifest = {"source_db": "default", "prefix": "", "dates": ["2026-10-05"]}
    monkeypatch.setattr(Ch, "scalar", lambda *args: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *args: [Case("q", "needle", "simple", ("",), "")])
    monkeypatch.setattr("dt_cloud.cli.err", lambda message: None)

    def compare(*args, **kwargs):
        calls.append(("canonical", kwargs))
        return {"body": {"ok": True}, "response_s": 1.0}

    def response(*args, **kwargs):
        calls.append(("optimized", kwargs))
        return {"body": {"ok": True}, "response_s": .5}

    monkeypatch.setattr(narrow_serve, "compare_response", compare)
    monkeypatch.setattr(narrow_serve, "response", response)
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", "-c", *(["-f"] if first else []),
                                      "-w", "900", "-n", "1", "-d", "2026-10-05", "-Q", "unused", "-T", str(tmp_path), "target"])
    assert result.exit_code == 0, result.output
    expected = [
        ("canonical", {"previous": None, "syntax": "simple", "reference_timeout": 900}),
        ("optimized", {"previous": None, "syntax": "simple", "threads": 8, "path_free": True, "name_index": False,
                       "parent_index": False, "name_index_variant": None, "ancestor_preaggregate": False}),
    ]
    assert calls == (expected if first else list(reversed(expected)))
    row = json.loads(result.stdout)
    assert (row["reference_timeout_s"], row["exact"], row["comparison_phase"]) == (900, True, "first" if first else "interleaved")
    assert sorted(tmp_path.iterdir()) == []


def test_server_deadline_cancels_temporary_ctas(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db, timeout=61, **statement_timeout_settings(1))
    observer = Ch(ch_url, session=False)
    query_id = f"reference_timeout_{uuid.uuid4().hex}"
    try:
        with pytest.raises(ChError) as caught:
            ch.tmp("deadline_test", "SELECT number, sleepEachRow(0.02) AS waited FROM numbers(1000)",
                   settings={"query_id": query_id, "max_block_size": 1})
        assert int(re.search(r"Code: (\d+)\.", caught.value.text).group(1)) == 159
        assert observer.scalar(f"SELECT count() FROM system.processes WHERE query_id = '{query_id}'") == "0"
        ch.close()
        assert ch.scalar("EXISTS TABLE deadline_test") == "0"
    finally:
        ch.close()
        observer.close()
