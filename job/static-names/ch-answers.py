"""Reference answers for `verify-answers`: `mega_names.answer(..., postings='m')` per (term, date) on the
ch-store VM's ClickHouse, one JSON line each with the nonzero buckets as `{bucket: [bytes, objects]}`.
Run on the VM (read-only): `job/static-names.sh ch-answers DATES TERMS_FILE`."""
import json
import sys

from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.mega_names import answer

dates = sys.argv[1].split(",")
terms = [t for t in open(sys.argv[2]).read().split("\n") if t]
for d in dates:
    for t in terms:
        ch = Ch("http://localhost:8123", db="default")
        try:
            body = answer(ch, d, t, postings="m", settings={"max_threads": 8})
        finally:
            ch.close()
        buckets = {b["path"]: [b["b"], b["o"]] for b in body["buckets"] if b["b"] or b["o"]}
        print(json.dumps({"date": d, "q": t.lower(), "buckets": buckets, "s": body["build_s"]}), flush=True)
