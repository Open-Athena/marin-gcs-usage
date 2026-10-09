from copy import deepcopy
from http.server import HTTPServer
from json import dumps, loads
from os import environ, pathsep
from pathlib import Path
from re import sub
from subprocess import run
from sys import executable
from types import SimpleNamespace
from threading import Thread
from urllib.parse import parse_qsl, urlsplit

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l1_http_bench as module
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from dt_cloud.chstore.hot_l1_publish import load, publish
from test_chhot_l1_batch_catalog import artifact, write

TOKEN_ENV = "HOT_L1_FIXTURE_TOKEN"
TOKEN = "private-fixture-token"
BASE = "http://fixture.invalid:8082"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "published"
    publish((write(tmp_path / "before.json", artifact("2026-10-04")), write(tmp_path / "after.json", artifact())), root)
    catalog = load(root)
    state = SimpleNamespace(root=root, catalog=catalog, calls=[], change=None, status=200, fail_at=None, raw=None, action=None, bodies=[])
    monkeypatch.setenv(TOKEN_ENV, TOKEN)

    def load_catalog(path: Path):
        state.calls.append(("load", path))
        return state.catalog

    def fetch(base: str, path: str, token: str | None, timeout: float) -> tuple:
        state.calls.append(("fetch", base, path, token, timeout))
        query = dict(parse_qsl(urlsplit(path).query))
        body = deepcopy(state.catalog.diff(query["from"], query["date"], query["name"]) if "from" in query else state.catalog.view(query["date"], query["name"]))
        if state.change is not None:
            state.change(body)
        data = state.raw if state.raw is not None else (dumps(body) + "\n").encode()
        state.bodies.append(data)
        index = len(state.bodies)
        if state.action is not None:
            state.action()
        return (state.status if state.fail_at is None or index == state.fail_at else 200), index * 10, {}, data

    monkeypatch.setattr(module, "load", load_catalog)
    monkeypatch.setattr(module, "fetch", fetch)
    return state


@pytest.mark.parametrize("diff", [False, True])
def test_every_response_exact_complete_body_and_compact_metrics_without_totals_or_token(fake: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture, diff: bool) -> None:
    out = tmp_path / "http.json"
    result = module.bench(fake.root, BASE, "2026-10-05", (".JSON", ".npy"), out,
                          token_env=TOKEN_ENV, compare_from="2026-10-04" if diff else None)
    sizes = [len(data) for data in fake.bodies]
    expected = {"schema": "hot-l1-http-bench-v1", "base_url": BASE, "catalog_root": str(fake.root), "date": "2026-10-05",
                "compare_from": "2026-10-04" if diff else None, "trials": 3, "responses": 6, "timeout_seconds": 30,
                "results": [{"pattern": ".JSON", "status": 200, "responses": 3,
                             "latency_ms": {"samples": [10, 20, 30], "median": 20, "p90": 30, "max": 30},
                             "response_bytes": {"samples": sizes[:3], "min": min(sizes[:3]), "max": max(sizes[:3])}},
                            {"pattern": ".npy", "status": 200, "responses": 3,
                             "latency_ms": {"samples": [40, 50, 60], "median": 50, "p90": 60, "max": 60},
                             "response_bytes": {"samples": sizes[3:], "min": min(sizes[3:]), "max": max(sizes[3:])}}],
                "validation": "every HTTP 200 body exactly equals the pinned registered reader; not an independent full-catalog scan",
                "p90_method": "nearest rank", "cache_state": "uncontrolled; resident root-only registered L1 HTTP responses"}
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    suffix = "&from=2026-10-04" if diff else ""
    assert fake.calls == [("load", fake.root)] + [
        ("fetch", BASE, f"/api/hot-l1?date=2026-10-05&name={name}{suffix}", TOKEN, 30)
        for name in (".JSON", ".npy") for _ in range(3)
    ]
    assert loads(fake.bodies[0])["pattern"] == ".json"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("status", [0, 401, 404, 500])
def test_failed_status_after_a_verified_response_leaves_no_accepted_artifact(fake: SimpleNamespace, tmp_path: Path, status: int) -> None:
    fake.status, fake.fail_at = status, 2
    out = tmp_path / "http.json"
    with pytest.raises(RuntimeError) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (".JSON",), out, token_env=TOKEN_ENV)
    assert str(caught.value) == f"hot L1 HTTP benchmark requires HTTP 200; received status {status}"
    assert fake.calls == [("load", fake.root)] + [("fetch", BASE, "/api/hot-l1?date=2026-10-05&name=.JSON", TOKEN, 30)] * 2
    assert out.exists() is False


@pytest.mark.parametrize("change", [lambda body: body.update(tier="extra-engine-label"),
                                     lambda body: body.pop("validation"),
                                     lambda body: body["buckets"][0].update(o=True)])
def test_extra_dropped_labels_and_equal_numeric_boolean_refuse_exact_body(fake: SimpleNamespace, tmp_path: Path, change: object) -> None:
    fake.change = change
    out = tmp_path / "http.json"
    with pytest.raises(AssertionError) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (".JSON",), out, token_env=TOKEN_ENV)
    assert str(caught.value) == "hot L1 HTTP response disagrees with the exact registered body"
    assert fake.calls == [("load", fake.root), ("fetch", BASE, "/api/hot-l1?date=2026-10-05&name=.JSON", TOKEN, 30)]
    assert out.exists() is False


@pytest.mark.parametrize("data", [b"not-json private response", b'{"a":1,"a":1}'])
def test_invalid_json_or_duplicate_keys_refuse_without_output(fake: SimpleNamespace, tmp_path: Path, data: bytes) -> None:
    fake.raw = data
    out = tmp_path / "http.json"
    with pytest.raises(ValueError):
        module.bench(fake.root, BASE, "2026-10-05", (".JSON",), out, token_env=TOKEN_ENV)
    assert out.exists() is False
    assert fake.calls == [("load", fake.root), ("fetch", BASE, "/api/hot-l1?date=2026-10-05&name=.JSON", TOKEN, 30)]


def test_all_requested_patterns_preflight_before_any_http(fake: SimpleNamespace, tmp_path: Path) -> None:
    from dt_cloud.chstore.hot_l1_catalog import CatalogRequest

    with pytest.raises(CatalogRequest) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (".json", "absent"), tmp_path / "http.json", token_env=TOKEN_ENV)
    assert str(caught.value) == "hot L1 batch pattern/date is not registered; no scan fallback"
    assert fake.calls == [("load", fake.root)]


@pytest.mark.parametrize("kwargs,message", [
    ({"trials": 0}, "hot L1 HTTP benchmark requires positive trials and a finite positive timeout"),
    ({"timeout": float("inf")}, "hot L1 HTTP benchmark requires positive trials and a finite positive timeout"),
    ({"base": "http://user:password@fixture.invalid"}, "HTTP benchmark requires an explicit HTTP(S) base URL without credentials/query/fragment"),
    ({"base": "http://fixture.invalid?token=secret"}, "HTTP benchmark requires an explicit HTTP(S) base URL without credentials/query/fragment"),
])
def test_invalid_inputs_before_catalog_or_network(fake: SimpleNamespace, tmp_path: Path, kwargs: dict, message: str) -> None:
    args = {"root": fake.root, "base": BASE, "date": "2026-10-05", "patterns": (".json",), "out": tmp_path / "http.json", "token_env": TOKEN_ENV, **kwargs}
    with pytest.raises(ValueError) as caught:
        module.bench(**args)
    assert str(caught.value) == message
    assert fake.calls == []


def test_missing_token_and_existing_artifact_preflight_preserve_output(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "http.json"
    monkeypatch.delenv(TOKEN_ENV)
    with pytest.raises(ValueError) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (".json",), out, token_env=TOKEN_ENV)
    assert str(caught.value) == "HTTP benchmark requires a nonempty single-line bearer token environment variable"
    out.write_text("existing\n")
    with pytest.raises(ValueError) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (".json",), out, token_env=TOKEN_ENV)
    assert str(caught.value) == "hot L1 HTTP benchmark output must be new"
    assert out.read_text() == "existing\n"
    assert fake.calls == []


def test_concurrent_output_writer_is_preserved(fake: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "http.json"
    fake.action = lambda: out.write_text("concurrent writer\n")
    with pytest.raises(FileExistsError):
        module.bench(fake.root, BASE, "2026-10-05", (".json",), out, token_env=TOKEN_ENV)
    assert out.read_text() == "concurrent writer\n"


@pytest.mark.parametrize("overrides", [False, True])
def test_http_bench_cli_exact_forwarding_and_compact_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: bool) -> None:
    from dt_cloud.cli import main

    calls, body = [], {"schema": "hot-l1-http-bench-v1", "responses": 6}

    def bench(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return body

    monkeypatch.setattr(module, "bench", bench)
    root, out = tmp_path / "published", tmp_path / "http.json"
    args = ["ch-hot-l1-http-bench", "-g", str(root), "-U", BASE, "-T", TOKEN_ENV, "-d", "2026-10-05", "-n", ".JSON", "-n", ".npy", "-o", str(out)]
    if overrides:
        args.extend(["-D", "2026-10-04", "-t", "7", "-w", "17"])
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(body) + "\n", "")
    assert calls == [((root, BASE, "2026-10-05", (".JSON", ".npy"), out), {
        "token_env": TOKEN_ENV, "compare_from": "2026-10-04" if overrides else None, "trials": 7 if overrides else 3,
        "timeout": 17 if overrides else 30,
        "all_registered": False,
    })]


def test_real_local_http_fetch_validates_complete_authenticated_body(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dt_cloud.chstore.bench import fetch
    from dt_cloud.chstore.hot_l1_http import make_handler

    monkeypatch.setattr(module, "fetch", fetch)
    out = tmp_path / "http.json"
    with HTTPServer(("127.0.0.1", 0), make_handler(fake.catalog, TOKEN)) as server:
        worker = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            result = module.bench(fake.root, base, "2026-10-05", (".JSON",), out, token_env=TOKEN_ENV, trials=1)
        finally:
            server.shutdown()
            worker.join(timeout=5)
    ms = result["results"][0]["latency_ms"]["samples"][0]
    assert type(ms) is int and ms >= 0
    size = len((dumps(fake.catalog.view("2026-10-05", ".JSON")) + "\n").encode())
    expected = {"schema": "hot-l1-http-bench-v1", "base_url": base, "catalog_root": str(fake.root), "date": "2026-10-05",
                "compare_from": None, "trials": 1, "responses": 1, "timeout_seconds": 30,
                "results": [{"pattern": ".JSON", "status": 200, "responses": 1,
                             "latency_ms": {"samples": [ms], "median": ms, "p90": ms, "max": ms},
                             "response_bytes": {"samples": [size], "min": size, "max": size}}],
                "validation": "every HTTP 200 body exactly equals the pinned registered reader; not an independent full-catalog scan",
                "p90_method": "nearest rank", "cache_state": "uncontrolled; resident root-only registered L1 HTTP responses"}
    assert result == expected
    assert loads(out.read_text()) == expected
    assert fake.calls == [("load", fake.root)]
    assert worker.is_alive() is False


@pytest.mark.parametrize("diff", [False, True])
def test_all_registered_in_order_with_complete_punctuation_encoding(fake: SimpleNamespace, tmp_path: Path, diff: bool) -> None:
    patterns = ("?&%=+", "\\\n", "å🙂")
    bodies = []
    for date in ("2026-10-04", "2026-10-05"):
        body = artifact(date)
        row = body["results"][0]
        body["results"] = [{**deepcopy(row), "predicate_id": q, "pattern": pattern} for q, pattern in enumerate(patterns, 1)]
        body["compiled_patterns"] = body["queries"]["patterns"] = len(patterns)
        body["validation"]["references"][0]["pattern"] = patterns[0]
        bodies.append(dumps(body).encode())
    fake.catalog = HotL1BatchCatalog.from_bytes(bodies)
    out = tmp_path / "all-http.json"
    result = module.bench(fake.root, BASE, "2026-10-05", (), out, token_env=TOKEN_ENV, all_registered=True,
                          compare_from="2026-10-04" if diff else None, trials=1)
    records = [{"pattern": pattern, "status": 200, "responses": 1,
                "latency_ms": {"samples": [q * 10], "median": q * 10, "p90": q * 10, "max": q * 10},
                "response_bytes": {"samples": [len(data)], "min": len(data), "max": len(data)}}
               for q, (pattern, data) in enumerate(zip(patterns, fake.bodies, strict=True), 1)]
    assert result == {"schema": "hot-l1-http-bench-v1", "base_url": BASE, "catalog_root": str(fake.root), "date": "2026-10-05",
                      "compare_from": "2026-10-04" if diff else None, "trials": 1, "responses": 3, "timeout_seconds": 30, "results": records,
                      "validation": "every HTTP 200 body exactly equals the pinned registered reader; not an independent full-catalog scan",
                      "p90_method": "nearest rank", "cache_state": "uncontrolled; resident root-only registered L1 HTTP responses", "all_registered": True}
    assert loads(out.read_text()) == result
    suffix = "&from=2026-10-04" if diff else ""
    assert fake.calls == [("load", fake.root)] + [
        ("fetch", BASE, f"/api/hot-l1?date=2026-10-05&name={encoded}{suffix}", TOKEN, 30)
        for encoded in ("%3F%26%25%3D%2B", "%5C%0A", "%C3%A5%F0%9F%99%82")
    ]


def test_all_registered_diff_missing_before_predicate_and_unknown_date_refuse_before_http(fake: SimpleNamespace, tmp_path: Path) -> None:
    from dt_cloud.chstore.hot_l1_catalog import CatalogRequest

    before = artifact("2026-10-04")
    before["results"] = before["results"][:1]
    before["compiled_patterns"] = before["queries"]["patterns"] = 1
    fake.catalog = HotL1BatchCatalog.from_bytes([dumps(before).encode(), dumps(artifact()).encode()])
    out = tmp_path / "all-http.json"
    with pytest.raises(CatalogRequest) as caught:
        module.bench(fake.root, BASE, "2026-10-05", (), out, token_env=TOKEN_ENV, all_registered=True, compare_from="2026-10-04")
    assert str(caught.value) == "hot L1 batch pattern/date is not registered; no scan fallback"
    with pytest.raises(CatalogRequest) as caught:
        module.bench(fake.root, BASE, "2026-10-03", (), out, token_env=TOKEN_ENV, all_registered=True)
    assert str(caught.value) == "hot L1 batch date is not registered; no scan fallback"
    assert fake.calls == [("load", fake.root), ("load", fake.root)]
    assert out.exists() is False


@pytest.mark.parametrize("patterns,all_registered,message", [
    ((".json",), True, "HTTP benchmark requires either explicit patterns or all_registered, not both"),
    ((), False, "HTTP benchmark requires either explicit patterns or all_registered, not both"),
    ((), 1, "HTTP benchmark all_registered must be a boolean"),
    ((), None, "HTTP benchmark all_registered must be a boolean"),
])
def test_selection_modes_are_exclusive_and_all_flag_requires_boolean(fake: SimpleNamespace, tmp_path: Path, patterns: tuple[str, ...], all_registered: object, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        module.bench(fake.root, BASE, "2026-10-05", patterns, tmp_path / "all-http.json", token_env=TOKEN_ENV, all_registered=all_registered)
    assert str(caught.value) == message
    assert fake.calls == []


def test_all_registered_cli_forwarding_and_module_help_before_main_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dt_cloud.cli import main

    calls, body = [], {"schema": "hot-l1-http-bench-v1", "all_registered": True, "responses": 4228}

    def bench(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return body

    monkeypatch.setattr(module, "bench", bench)
    root, out = tmp_path / "published", tmp_path / "all-http.json"
    args = ["ch-hot-l1-http-bench", "-a", "-g", str(root), "-U", BASE, "-T", TOKEN_ENV, "-d", "2026-10-05", "-t", "1", "-o", str(out)]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(body) + "\n", "")
    assert calls == [((root, BASE, "2026-10-05", (), out), {"token_env": TOKEN_ENV, "compare_from": None, "trials": 1, "timeout": 30, "all_registered": True})]
    imported = CliRunner().invoke(main, ["ch-hot-l1-http-bench", "-a", "--help"], terminal_width=80)
    project = Path(__file__).resolve().parents[2]
    completed = run(
        [executable, "-m", "dt_cloud.cli", "ch-hot-l1-http-bench", "-a", "--help"],
        cwd=project,
        env={**environ, "COLUMNS": "80", "PYTHONPATH": pathsep.join((str(project / "cloud/src"), str(project / "src"), environ.get("PYTHONPATH", "")))},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    def normalize(value: str) -> str:
        # Click detects different pipe/CliRunner widths; retain every help token.
        return " ".join(sub(r"\AUsage: .*? ch-hot-l1-http-bench ", "Usage: <cli> ch-hot-l1-http-bench ", value).split())

    assert (imported.exit_code, imported.stderr) == (0, "")
    assert (completed.returncode, normalize(completed.stdout), completed.stderr) == (imported.exit_code, normalize(imported.stdout), imported.stderr)


@pytest.mark.parametrize("selection", [[], ["-a", "-n", ".json"]])
def test_cli_requires_exactly_one_selection_mode_before_benchmark(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: list[str]) -> None:
    from dt_cloud.cli import main

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid CLI selection reached benchmark")

    monkeypatch.setattr(module, "bench", forbidden)
    args = ["ch-hot-l1-http-bench", "-g", str(tmp_path), "-U", BASE, "-T", TOKEN_ENV, "-d", "2026-10-05", "-o", str(tmp_path / "out.json"), *selection]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, " ".join(result.stderr.split())) == (
        2, "", "Usage: main ch-hot-l1-http-bench [OPTIONS] Try 'main ch-hot-l1-http-bench --help' for help. Error: HTTP benchmark requires either --pattern or --all-registered, not both",
    )
