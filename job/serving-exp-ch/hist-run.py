#!/usr/bin/env python3
"""On the ClickHouse VM (as root): time each `-- qN` query of a SQL file cold
(OS page cache and ClickHouse's caches dropped first) and then warm (best of
2), via clickhouse-client. Prints one TSV line per query: id, cold s, warm s,
rows read, peak memory (from the query log).

    hist-run.py hist-queries.sql [--param_deep=…]
"""
import re
import subprocess
import sys
import time

DROPS = ["MARK CACHE", "UNCOMPRESSED CACHE", "INDEX MARK CACHE", "INDEX UNCOMPRESSED CACHE", "QUERY CONDITION CACHE",
         "PRIMARY INDEX CACHE", "PAGE CACHE", "MMAP CACHE"]


def ch(sql: str, args: list[str]) -> float:
    t = time.monotonic()
    subprocess.run(["clickhouse-client", "--multiquery", *args, "-q", sql], check=True, stdout=subprocess.DEVNULL)
    return time.monotonic() - t


def cold() -> None:
    subprocess.run(["clickhouse-client", "--multiquery", "-q", "; ".join(f"SYSTEM DROP {d}" for d in DROPS)], check=True)
    subprocess.run(["sync"], check=True)
    with open("/proc/sys/vm/drop_caches", "w") as f:
        f.write("3\n")


def main() -> None:
    path, args = sys.argv[1], sys.argv[2:]
    text = open(path).read()
    head = "SET max_threads = 8;"
    blocks = re.split(r"\n(?=-- q\d+ )", text)
    for b in blocks:
        m = re.match(r"-- (q\d+) (.*)", b)
        if not m:
            continue
        sql = "\n".join(line for line in b.splitlines() if not line.startswith("--")).strip()
        tag = f"/* {m.group(1)} */ "
        cold()
        c = ch(f"{head} {tag}{sql}", args)
        w = min(ch(f"{head} {tag}{sql}", args) for _ in range(2))
        subprocess.run(["clickhouse-client", "-q", "SYSTEM FLUSH LOGS"], check=True)
        stats = subprocess.run(["clickhouse-client", "-q", f"""SELECT max(read_rows), formatReadableSize(max(memory_usage)) FROM system.query_log
            WHERE type = 'QueryFinish' AND query LIKE '%/* {m.group(1)} */%' AND event_time > now() - INTERVAL 1 HOUR AND query NOT LIKE '%query_log%'"""],
            check=True, capture_output=True, text=True).stdout.strip()
        print(f"{m.group(1)}\t{c:.2f}\t{w:.2f}\t{stats}\t{m.group(2)}", flush=True)


main()
