#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pyarrow", "google-cloud-storage", "click"]
# ///
"""Answer a name-substring query from a suffix-ordered postings parquet on GCS,
the way a Worker would: the footer (in production, a sidecar in KV/D1) is
fetched once and cached; each query then reads ONE contiguous byte range, the
row groups whose suffix range overlaps the query's, and filters/aggregates in
process. Prints per-bucket first-hit bytes/objects plus the I/O it took."""
from __future__ import annotations

import json
import sys
from bisect import bisect_left, bisect_right
from io import RawIOBase
from pathlib import Path
from time import monotonic

import click
import pyarrow.parquet as pq
from google.cloud import storage

FOREVER = 4291747200  # 2106-01-01, the store's "still open"


class Spans(RawIOBase):
    """A seekable file over a few cached byte spans; any read outside them is an error (we count every fetch)."""

    def __init__(self, size: int, spans: list[tuple[int, bytes]]):
        self.size, self.spans, self.pos = size, spans, 0

    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        for start, data in self.spans:
            if start <= self.pos and self.pos + n <= start + len(data):
                b[:n] = data[self.pos - start:self.pos - start + n]
                self.pos += n
                return n
        raise IOError(f"read outside cached spans: {self.pos}+{n}")


def footer(blob) -> tuple[int, bytes]:
    blob.reload()
    size = blob.size
    tail = blob.download_as_bytes(start=size - 8, end=size - 1)
    flen = int.from_bytes(tail[:4], "little")
    return size, blob.download_as_bytes(start=size - 8 - flen, end=size - 1)


@click.command()
@click.option('-d', '--date', 'dates', multiple=True, required=True, help="Scan date(s) YYYY-MM-DD")
@click.option('-s', '--since', 'sinces', multiple=True, required=True, help="The scan's epoch start (`scan_bound`), one per date")
@click.option('-u', '--url', required=True, help="gs://bucket/path.parquet")
@click.argument('terms', nargs=-1)
def main(dates, sinces, url, terms):
    bucket, name = url[5:].split('/', 1)
    blob = storage.Client().bucket(bucket).blob(name)
    t0 = monotonic()
    size, foot = footer(blob)
    md = pq.ParquetFile(Spans(size, [(size - len(foot), foot)])).metadata
    t_foot = monotonic() - t0
    s_col = md.schema.names.index('s')
    groups = []
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        st = rg.column(s_col).statistics
        lo = min(rg.column(j).dictionary_page_offset or rg.column(j).data_page_offset for j in range(rg.num_columns))
        hi = max((rg.column(j).dictionary_page_offset or rg.column(j).data_page_offset) + rg.column(j).total_compressed_size for j in range(rg.num_columns))
        groups.append((st.min, st.max, lo, hi, rg.num_rows))
    mins = [g[0] for g in groups]
    maxs = [g[1] for g in groups]
    print(json.dumps({"footer_bytes": len(foot), "footer_s": round(t_foot, 3), "row_groups": len(groups), "file_bytes": size}), file=sys.stderr)
    for q in terms:
        q = q.lower()
        key = q[:24]
        # Row groups that can hold suffixes starting with `key`: from the first whose max reaches it (maxes are
        # sorted too; several groups may start with exactly `key`) to the last whose min is below its successor.
        a = bisect_left(maxs, key)
        b = bisect_left(mins, key + '\U0010ffff')
        out = {"q": q, "groups": b - a}
        if a >= b:
            out.update(bytes=0, rows=0, fetch_s=0)
            print(json.dumps(out)); continue
        lo, hi = groups[a][2], groups[b - 1][3]
        t1 = monotonic()
        data = blob.download_as_bytes(start=lo, end=hi - 1)
        out["fetch_s"] = round(monotonic() - t1, 3)
        out["bytes"] = len(data)
        f = pq.ParquetFile(Spans(size, [(size - len(foot), foot), (lo, data)]))
        t2 = monotonic()
        tab = f.read_row_groups(list(range(a, b)), columns=['s', 'depth', 'path', 'usr', 'vf', 'vt', 'size', 'n_files'])
        rows = tab.to_pylist()
        out["rows_read"] = len(rows)
        hit = [r for r in rows if r['s'].startswith(key) and q in r['path'].rsplit('/', 1)[-1].lower()]
        out["rows_matching"] = len(hit)
        per_date = {}
        for D, since in zip(dates, sinces):
            Dt, St = _epoch(D), _epoch(since)
            seen, totals = set(), {}
            for r in hit:
                vf, vt = int(r['vf'].timestamp()), int(r['vt'].timestamp())
                if not (St <= vf <= Dt < vt) or r['depth'] < 1:
                    continue
                k = (r['path'], r['usr'], vf)
                if k in seen:
                    continue
                seen.add(k)
                parent = r['path'].rsplit('/', 1)[0] if '/' in r['path'] else ''
                if q in parent.lower():
                    continue
                bkt = r['path'].split('/', 1)[0]
                b_, o_ = totals.get(bkt, (0, 0))
                totals[bkt] = (b_ + r['size'], o_ + r['n_files'])
            per_date[D] = {"live_rows": len(seen), "buckets": totals}
        out["compute_s"] = round(monotonic() - t2, 3)
        out["answers"] = per_date
        print(json.dumps(out))


def _epoch(d: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


if __name__ == '__main__':
    main()
