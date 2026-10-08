"""`mega_names.append`: daily upkeep of the consolidated name index. A store
indexed through 09-30 that then ingests 10-01 and appends it holds exactly the
postings and spans a full rebuild over all three scans does, answers every
substring on every day like brute force, and refuses to append a scan twice."""

import pytest

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import mega_names
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.coarse import CoarseRequest

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import write_v2
from test_chmega_names import brute, patterns
from test_chstore import DAYS, src_rows

LAST = "2026-10-01"
STEMS = {"all": None, "since0930": "2026-09-30"}


@pytest.fixture(scope="module")
def store(ch_url, ch_db, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("days")
    files = {day: write_v2(d / day, fs)[0] for day, fs in DAYS.items()}
    ch = Ch(ch_url, db=ch_db)
    ingest = lambda day: ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None).run()
    for day in DAYS:
        if day != LAST:
            ingest(day)
    mega_names.build_spans(ch)
    for stem, start in STEMS.items():
        mega_names.build_postings(ch, stem, start)
    ingest(LAST)
    appended = mega_names.append(ch, LAST, list(STEMS))
    return {"ch": ch, "rows": {day: src_rows(files[day]) for day in DAYS}, "appended": appended}


def test_append_equals_full_rebuild(store):
    ch = store["ch"]
    incremental = {stem: mega_names.digest(ch, stem) for stem in ("name_spans", *STEMS)}
    mega_names.build_spans(ch)
    for stem, start in STEMS.items():
        mega_names.build_postings(ch, f"full_{stem}", start)
    full = {"name_spans": mega_names.digest(ch, "name_spans"),
            **{stem: {t.removeprefix("full_"): v for t, v in mega_names.digest(ch, f"full_{stem}").items()} for stem in STEMS}}
    assert incremental == full
    assert {stem: sorted(body["rows"]) for stem, body in store["appended"]["targets"].items()} == {
        "name_spans": ["name_spans"],
        "all": ["all_closures", "all_nodes"],
        "since0930": ["since0930_closures", "since0930_nodes"],
    }


def test_appended_index_answers_every_substring(store):
    ch, rows = store["ch"], store["rows"]
    mismatches = [(stem, day, p) for stem, start in STEMS.items() for day in DAYS if start is None or day >= start
                  for p in patterns(rows)
                  if {k: v for k, v in mega_names.answer(ch, day, p, postings=stem).items() if k in ("root", "buckets")} != brute(rows[day], p)]
    assert mismatches == []


def test_append_refuses_a_covered_scan(store):
    with pytest.raises(CoarseRequest) as caught:
        mega_names.append(store["ch"], LAST, ["all"])
    assert str(caught.value) == "`name_spans` already covers 2026-10-01 (logged through 2026-10-01 00:00:00)"


def test_build_through_an_earlier_scan_then_append_equals_full(store):
    """The production verification path: rebuild through 09-30 after 10-01 is already ingested, then append it."""
    ch = store["ch"]
    mega_names.build_postings(ch, "upto", None, end="2026-09-30")
    mega_names.build_postings(ch, "whole", None)
    mega_names.build_spans(ch, end="2026-09-30")
    before = mega_names.digest(ch, "upto")
    with pytest.raises(CoarseRequest) as caught:
        mega_names.append(ch, LAST, ["upto", "nope"])
    assert (str(caught.value), mega_names.digest(ch, "upto")) == ("`nope` has no build in `name_index_log`; build it before appending", before)
    assert mega_names.append(ch, LAST, ["upto"])["date"] == LAST
    assert ({t.removeprefix("upto_"): v for t, v in mega_names.digest(ch, "upto").items()}
            == {t.removeprefix("whole_"): v for t, v in mega_names.digest(ch, "whole").items()} != {t.removeprefix("upto_"): v for t, v in before.items()})
