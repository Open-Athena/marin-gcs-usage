"""`mega_names`: name-substring bucket totals for any scan in the consolidated
store equal a brute-force first-hit scan of that day's source rows, for every
substring of the fixture's names, on every ingested day (including a day that
closes paths and one that reopens an owner slice)."""

import pytest

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import mega_names
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.coarse import CoarseRequest

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import write_v2
from test_chstore import DAYS, src_rows


def parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def brute(rows: list[tuple], pattern: str) -> dict:
    """First hits over every owner slice: the name matches, the parent path doesn't."""
    out: dict[str, list[int]] = {}
    for depth, path, _usr, _kind, size, n_files, *_ in rows:
        if depth == 0:
            continue
        bucket = path.split("/")[0]
        out.setdefault(bucket, [0, 0])
        if pattern in path.rsplit("/", 1)[-1].lower() and pattern not in parent(path).lower():
            out[bucket][0] += size
            out[bucket][1] += n_files
    return {"root": {"b": sum(b for b, _ in out.values()), "o": sum(o for _, o in out.values())},
            "buckets": [{"path": p, "b": b, "o": o} for p, (b, o) in sorted(out.items())]}


@pytest.fixture(scope="module")
def store(ch_url, ch_db, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("days")
    files = {day: write_v2(d / day, fs)[0] for day, fs in DAYS.items()}
    ch = Ch(ch_url, db=ch_db)
    for day in DAYS:
        ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None).run()
    return {"ch": ch, "rows": {day: src_rows(files[day]) for day in DAYS}}


def patterns(rows_by_day: dict) -> list[str]:
    names = {path.rsplit("/", 1)[-1].lower() for rows in rows_by_day.values() for _, path, *_ in rows if path}
    return sorted({n[i:i + k] for n in names for k in (1, 2, 3) for i in range(len(n) - k + 1)} | {"zzz", "u1", "ckpt", "huge.bin"})


def test_every_substring_on_every_day_equals_brute_force(store):
    ch, rows = store["ch"], store["rows"]
    mismatches = []
    for day in DAYS:
        for p in patterns(rows):
            got = mega_names.answer(ch, day, p)
            if {"root": got["root"], "buckets": got["buckets"]} != brute(rows[day], p):
                mismatches.append((day, p))
    assert mismatches == []


def test_case_insensitive_and_shape(store):
    body = mega_names.answer(store["ch"], "2026-09-30", "CKPT")
    assert ({k: body[k] for k in ("schema", "date", "pattern", "exact")}, body["buckets"]) == (
        {"schema": "mega-name-totals-v1", "date": "2026-09-30", "pattern": "ckpt", "exact": True},
        brute(store["rows"]["2026-09-30"], "ckpt")["buckets"],
    )


@pytest.mark.parametrize("pattern,error", [
    ("a/b", "name totals need one nonempty literal without slashes or NUL, at most 512 characters"),
    ("", "name totals need one nonempty literal without slashes or NUL, at most 512 characters"),
])
def test_refuses_bad_literals(store, pattern, error):
    with pytest.raises(CoarseRequest) as caught:
        mega_names.answer(store["ch"], "2026-09-30", pattern)
    assert str(caught.value) == error


def test_refuses_unpublished_scan_and_budgets(store):
    with pytest.raises(CoarseRequest) as caught:
        mega_names.answer(store["ch"], "2026-09-01", "ckpt")
    assert str(caught.value) == "no published scan on 2026-09-01"
    with pytest.raises(CoarseRequest) as caught:
        mega_names.answer(store["ch"], "2026-09-30", ".", max_names=2)
    assert str(caught.value) == "vocabulary exceeds its 2-name work budget"
