"""The store's answers (`dt_cloud.chstore.serve`, specs/ch-store.md §4)
against the Worker's bodies (`box-parity.json`: filtered subtrees and diffs,
plain subtrees and diffs, series over the `v2-search` fixture) and against
the `mem` box's on the generations `test_box` builds."""

import json

import pytest

from dt_cloud.bench.query import parse_simple
from dt_cloud.box import view as bv
from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import serve as cs
from dt_cloud.chstore.client import Ch

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_bench_truth import PATH_F
from test_box import GOLDEN, body as box_body, fixture_ix, own, two, write_v2  # noqa: F401 — fixtures
from test_chstore import A as DAY_A, B as DAY_B

SA, SB, SC = "2026-10-01T0001", "2026-10-01T0003", "2026-10-02T0001"
PATH_C = PATH_F.replace("/v2-search/", "/v2-search-b/")
DROP = {"tier", "index", "partial", "partialReason", "approximate", "approximateReason"}


def ingest_all(ch: Ch, days: dict[str, str]) -> cs.Store:
    for day, pf in days.items():
        ci.Ingest(ch, day, pf, threads=2, log=lambda *a: None).run()
    return cs.Store(ch.url, db=ch.db, threads=2, root_label="root")


@pytest.fixture(scope="module")
def fx(ch_url, ch_db):  # noqa: F811
    """The `v2-search` generation as two scans (the Worker's parity test seeds
    both dates with it), then `v2-search-b` a scan later."""
    return ingest_all(Ch(ch_url, db=ch_db), {SA: PATH_F, SB: PATH_F, SC: PATH_C})


def subtree(st: cs.Store, date: str, path: str, q: str | None, **kw) -> dict:
    w, h = kw.pop("w", 1280), kw.pop("h", 896)
    ma, at = kw.pop("min_area", 12), kw.pop("atten", 2)
    md = kw.pop("max_depth", None)
    ch = st.session()
    s = st.scan(date)
    if q is None:
        v = cs.plain_view(ch, s, path, w=w, h=h, min_area=ma, atten=at, max_depth=md)
    else:
        pr = cs.filter_prepare(ch, s, path, parse_simple(q, min_term=1))
        v = cs.filter_view(ch, pr, w=w, h=h, min_area=ma, atten=at, max_depth=md) if pr else None
    return json.loads("".join(cs.subtree_body(ch, v, date=date, path=path, w=w, h=h, min_area=ma, atten=at, q=q, root_label=st.root_label)))


def diff(st: cs.Store, a: str, b: str, path: str, q: str | None, **kw) -> dict:
    ch = st.session()
    ast = parse_simple(q, min_term=1) if q is not None else None
    return json.loads("".join(cs.diff_body(ch, st.scan(a), st.scan(b), path=path, w=kw.pop("w", 1280), h=kw.pop("h", 896), min_area=kw.pop("min_area", 12),
                                           atten=kw.pop("atten", 2), top=kw.pop("top", 500), ast=ast, q=q, depth=kw.pop("depth", None),
                                           summary=kw.pop("summary", False))))


def strip(b: dict) -> dict:
    return {k: v for k, v in b.items() if k not in DROP and v is not None}


@pytest.mark.parametrize("i", range(len(GOLDEN["subtree"])))
def test_filtered_subtree(i, fx, fixture_ix):  # noqa: F811
    """Each filtered case equals the Worker's body, and the `mem` box's."""
    c = GOLDEN["subtree"][i]["case"]
    kw = dict(w=c.get("w", 1280), h=c.get("h", 896), min_area=c.get("minArea", 12), atten=c.get("atten", 2), max_depth=c.get("depth"))
    got = strip(subtree(fx, SA, c["path"], c["q"], **kw))
    assert got == strip(GOLDEN["subtree"][i]["body"])
    assert got == strip(box_body(fixture_ix, c["path"], c["q"], date=SA, **kw))


@pytest.mark.parametrize("i", range(len(GOLDEN["diff"])))
def test_filtered_diff(i, fx):
    e = GOLDEN["diff"][i]
    c, want = e["case"], e["body"]
    got = diff(fx, SA, SB, c["path"], c["q"], depth=c.get("depth"))
    assert strip(got) == strip(want)


@pytest.mark.parametrize("i", range(len(GOLDEN["plain"])))
def test_plain_subtree(i, fx):
    e = GOLDEN["plain"][i]
    c, want = e["case"], e["body"]
    got = subtree(fx, SA, c["path"], None, w=c.get("w", 1280), h=c.get("h", 896), min_area=c.get("minArea", 12), atten=c.get("atten", 2), max_depth=c.get("depth"))
    assert strip(got) == strip(want)


@pytest.mark.parametrize("i", range(len(GOLDEN["diffC"])))
def test_diff_changes_parity(i, fx):
    """Diffs with changes (A → C), plain and filtered, equal the Worker's."""
    e = GOLDEN["diffC"][i]
    c, want = e["case"], e["body"]
    got = diff(fx, SA, SC, c["path"], c.get("q"), w=c.get("w", 1280), h=c.get("h", 896), min_area=c.get("minArea", 12), atten=c.get("atten", 2),
               top=c.get("top", 500), depth=c.get("depth"), summary=c.get("summary", False))
    assert strip(got) == strip(want)


@pytest.mark.parametrize("i", range(len(GOLDEN["series"])))
def test_series_parity(i, fx):
    e = GOLDEN["series"][i]
    c, want = e["case"], e["body"]
    got = json.loads(cs.series_body(fx.session(), list(fx.scans().values()), path=c["path"], paths=c.get("paths", []), split=c.get("split", False)))
    assert got == want


def test_diff_with_changes(ch_url, two, tmp_path_factory):  # noqa: F811
    """`test_box.test_diff_changes`'s generations as two scans: the same body."""
    d = tmp_path_factory.mktemp("two")
    days = {"2026-09-30": write_v2(d / "a", DAY_A)[0], "2026-10-01": write_v2(d / "b", DAY_B)[0]}
    import uuid

    db = f"t_{uuid.uuid4().hex[:10]}"
    Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
    try:
        st = ingest_all(Ch(ch_url, db=db), days)
        got = diff(st, "2026-09-30", "2026-10-01", "b", "ckpt")
        want = json.loads("".join(bv.diff_body(two[0], two[1], prev="2026-09-30", curr="2026-10-01", path="b", w=1280, h=896, min_area=12, atten=2,
                                               top=500, ast=parse_simple("ckpt"), q="ckpt")))
        assert {k: v for k, v in got.items() if k != "tier"} == {k: v for k, v in want.items() if k != "tier"}
    finally:
        Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")


# --- the server (`serve-query -e ch`) ----------------------------------------------------


def start(box) -> tuple[str, object]:
    import threading
    from http.server import ThreadingHTTPServer

    from dt_cloud.box import server as bs

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bs.make_handler(box, "s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


@pytest.fixture(scope="module")
def server(fx):
    from dt_cloud.box import server as bs

    url, httpd = start(bs.ChBox(fx))
    yield url
    httpd.shutdown()


def get(url: str, token: str | None = "s3cret") -> tuple[int, str, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers["x-query-engine"].split(";")[0], r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.headers["x-query-engine"] or "").split(";")[0], e.read().decode()


def test_server(server, fx):
    st, eng, b = get(f"{server}/healthz", token=None)
    assert (st, eng, json.loads(b)) == (200, "box", {"state": "ready", "engine": "ch", "scans": [
        {"date": SA, "version": 2}, {"date": SB, "version": 2}, {"date": SC, "version": 2}]})
    # The Worker's bodies: a plain subtree (w/h quantized up as the Worker does), a filtered one, a diff, a series.
    st, eng, b = get(f"{server}/api/subtree?date={SA}&path=bk&w=1200&h=850")
    assert (st, eng, strip(json.loads(b))) == (200, "box", strip(GOLDEN["plain"][1]["body"]))
    st, _, b = get(f"{server}/api/subtree?date={SA}&path=bk/tmp&q=ttl&h=896")
    assert (st, strip(json.loads(b))) == (200, strip(subtree(fx, SA, "bk/tmp", "ttl")))
    st, _, b = get(f"{server}/api/diff?from={SA}&to={SC}&path=&h=896")
    assert (st, strip(json.loads(b))) == (200, strip(GOLDEN["diffC"][0]["body"]))
    st, _, b = get(f"{server}/api/series?path=bk&n=3&first={SA}&last={SC}")
    assert (st, json.loads(b)) == (200, GOLDEN["series"][1]["body"])
    assert [get(f"{server}{u}", token=t) for u, t in (
        (f"/api/subtree?date={SA}&path=bk", None),
        ("/api/subtree?date=2026-09-01&path=bk", "s3cret"),
        (f"/api/diff?from=2026-09-30&to={SA}", "s3cret"),
        (f"/api/subtree?date={SA}&path=nope", "s3cret"),
        (f"/api/diff?from={SA}&to={SC}&path=nope", "s3cret"),
        (f"/api/subtree?date={SA}&path=bk&q=ab", "s3cret"),
        (f"/api/subtree?date={SA}&q=a/.*json&qs=regex", "s3cret"),
        (f"/api/subtree?date={SA}&lens=user:alice", "s3cret"),
        (f"/api/subtree?date={SA}&o=unowned", "s3cret"),
        (f"/api/series?path=bk&n=4&first={SA}&last={SC}", "s3cret"),
        ("/api/series?path=bk&split=roots", "s3cret"),
        ("/api/subtree?date=bad", "s3cret"),
    )] == [
        (401, "box", "unauthorized"),
        (409, "box", "scan 2026-09-01 not in the store"),
        (409, "box", "scan 2026-09-30 not in the store"),
        (404, "box", "path not found"),
        (404, "box", "path not found in either scan"),
        (400, "box", "bad query: type at least 3 characters (“ab”)"),
        (501, "box", "not supported by the box: regex 'a/.*json': no name filter (its tail can cross a `/` or isn't `$`-anchored)"),
        (409, "box", "a user lens isn't served by the box"),
        (501, "box", "owner / class scopes aren't served by the ch engine"),
        (409, "box", f"the store holds 3 scans in [{SA}, {SC}], not the Worker's 4"),
        (400, "box", "split=roots is for the unscoped store root only"),
        (400, "box", "bad date"),
    ]


def test_server_store_down():
    """ClickHouse unreachable: a 503 the Worker falls back on, and an `error` health."""
    from dt_cloud.box import server as bs

    url, httpd = start(bs.ChBox(cs.Store("http://127.0.0.1:9", root_label="root")))
    try:
        st, _, b = get(f"{url}/healthz", token=None)
        assert (st, json.loads(b)["state"]) == (500, "error")
        assert get(f"{url}/api/subtree?date={SA}&path=bk") == (503, "box", "backend error: URLError")
    finally:
        httpd.shutdown()
