"""Exact frozen-fleet L1 coverage without enumerating matching IDs in Python.

Own leaf contributions are disjoint from the outer matching directory
rollups. Temporary indexes belong to the supplied session and disappear at
`Ch.close()`. This is an offline aggregate, not a persistent serving index.
"""

from json import loads
from time import monotonic
from uuid import uuid4

from ..bench.ch import like_lit
from .client import Ch, lit
from .coarse import CoarseRequest
from .narrow import disk_reserve, identifier


def _buckets(ch: Ch, target: str) -> list[list]:
    rows = ch.json(f"SELECT toUInt64(pre), toUInt64(post), path FROM {target}.dictionary WHERE depth = 1 ORDER BY pre LIMIT 7")
    if not 1 <= len(rows) <= 6:
        raise CoarseRequest("hot L1 requires one to six complete global bucket intervals")
    root = ch.json(f"SELECT pre, post FROM {target}.dictionary WHERE depth = 0 AND path = ''")
    if (len(root) != 1 or len({p for _, _, p in rows}) != len(rows) or
            any(not p or "/" in p or lo > hi for lo, hi, p in rows) or
            rows[0][0] != root[0][0] + 1 or rows[-1][1] != root[0][1] or
            any(a[1] + 1 != b[0] for a, b in zip(rows, rows[1:]))):
        raise CoarseRequest("hot L1 bucket intervals do not partition the global dictionary")
    return rows


def _bounds_sql(rows: list[list]) -> str:
    return " UNION ALL ".join(
        f"SELECT toUInt64({lo}) AS pre, toUInt64({hi}) AS post, {lit(path)} AS path, toUInt8(0) AS shard"
        for lo, hi, path in rows
    )


def _complete(rows: list[list], totals: list[list]) -> list[dict]:
    by_pre = {pre: (b, o) for pre, b, o in totals}
    if len(by_pre) != len(totals) or not set(by_pre).issubset({lo for lo, _, _ in rows}):
        raise RuntimeError("hot L1 aggregation returned duplicate or unknown buckets")
    return [{"pre": lo, "post": hi, "path": path, "b": by_pre.get(lo, (0, 0))[0], "o": by_pre.get(lo, (0, 0))[1]}
            for lo, hi, path in sorted(rows, key=lambda row: row[2])]


def validate_caps(max_names: int | None, max_postings: int | None, max_roots: int | None) -> None:
    for cap, label in ((max_names, 'vocabulary'), (max_postings, 'direct-posting')):
        if cap is not None and (type(cap) is not int or cap <= 0):
            raise CoarseRequest(f'hot L1 {label} budget must be a positive integer when supplied')
    if max_roots is not None and (type(max_roots) is not int or max_roots <= 0):
        raise CoarseRequest('hot L1 outer-directory budget must be positive when supplied')


def build(
    ch: Ch,
    target: str,
    date: str,
    pattern: str,
    *,
    max_names: int | None = None,
    max_postings: int | None = None,
    max_roots: int | None = None,
    min_free_bytes: int = 20 << 30,
) -> dict:
    """Complete immediate-bucket bytes/objects for one slash-free literal.

    Requires an audited immutable global frozen dictionary/snapshot. The
    caller chooses offline memory, spill and statement limits; no vocabulary
    sampling or cap is substituted for a complete accepted answer. Optional
    name/direct-posting caps refuse before directory sorting. They bound
    accepted logical rows, not index-granule reads or total request time.
    """
    identifier(target)
    if not isinstance(pattern, str) or not pattern or "/" in pattern or len(pattern) > 512:
        raise CoarseRequest("hot L1 requires one nonempty literal without slashes, at most 512 characters")
    if '\0' in pattern:
        raise CoarseRequest('hot L1 requires a valid UTF-8 literal without NUL')
    try:
        pattern.encode('utf-8')
    except UnicodeEncodeError:
        raise CoarseRequest('hot L1 requires a valid UTF-8 literal without NUL') from None
    validate_caps(max_names, max_postings, max_roots)
    start = monotonic()
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "":
        raise CoarseRequest("hot L1 requires a global frozen target")
    if date not in manifest["dates"]:
        raise CoarseRequest("scan outside the frozen index")
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    pattern = pattern.lower()
    buckets = _buckets(ch, target)
    tag = uuid4().hex
    names, candidates, roots, bounds = (f"hot_l1_{part}_{tag}" for part in ("names", "directories", "roots", "buckets"))
    ch.tmp(bounds, _bounds_sql(buckets))
    stages = {"vocabulary_s": 0.0, "directory_roots_s": 0.0, "aggregate_s": 0.0}
    posting_count = None
    if max_postings is not None:
        stages['postings_s'] = 0.0
    shortcut = all(pattern in path.lower() for _, _, path in buckets)
    if shortcut:
        stage = monotonic()
        totals = ch.json(f"SELECT pre, b, o FROM {db}.nodes WHERE pre IN ({','.join(str(lo) for lo, _, _ in buckets)})")
        stages["aggregate_s"] = round(monotonic() - stage, 6)
        vocabulary_count = nonleaf_count = root_count = 0
    else:
        disk_reserve(ch, "hot L1 vocabulary", min_free_bytes)
        stage = monotonic()
        name_limit = f' LIMIT {max_names + 1}' if max_names is not None else ''
        ch.tmp(names, f"SELECT nid FROM {target}.names WHERE l LIKE {like_lit(pattern)}{name_limit}", disk=True, order_by="nid")
        vocabulary_count = int(ch.scalar(f"SELECT count() FROM {names}"))
        if max_names is not None and vocabulary_count > max_names:
            raise CoarseRequest(f'hot L1 vocabulary exceeds its {max_names:,}-name work budget')
        stages["vocabulary_s"] = round(monotonic() - stage, 6)
        stage = monotonic()
        source = f"SELECT toUInt64(pre) AS pre, toUInt64(post) AS post, b, o FROM {db}.nodes_by_name WHERE nid IN (SELECT nid FROM {names})"
        if max_postings is not None:
            # LIMIT precedes any preorder sort. Count the sentinel, then reuse
            # this complete owned stage for both matching dirs and leaves.
            postings = f'hot_l1_postings_{tag}'
            disk_reserve(ch, 'hot L1 direct postings', min_free_bytes)
            ch.tmp(postings, source + f' LIMIT {max_postings + 1}', disk=True, ordered=False)
            posting_count = int(ch.scalar(f'SELECT count() FROM {postings}'))
            if posting_count > max_postings:
                raise CoarseRequest(f'hot L1 direct-posting set exceeds its {max_postings:,}-row work budget')
            stages['postings_s'] = round(monotonic() - stage, 6)
            source = f'SELECT pre, post, b, o FROM {postings}'
            stage = monotonic()
        disk_reserve(ch, "hot L1 matching directories", min_free_bytes)
        ch.tmp(candidates, f"SELECT * FROM ({source}) WHERE pre != post", disk=True, order_by="pre")
        nonleaf_count = int(ch.scalar(f"SELECT count() FROM {candidates}"))
        ch.tmp(roots, f"""SELECT pre, post, b, o FROM (
            SELECT *, row_number() OVER (ORDER BY pre) AS rn,
                max(post) OVER (ORDER BY pre ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_end
            FROM {candidates}
        ) WHERE rn = 1 OR pre > previous_end""", disk=True, order_by="pre")
        root_count = int(ch.scalar(f"SELECT count() FROM {roots}"))
        if max_roots is not None and root_count > max_roots:
            raise CoarseRequest(f"hot L1 outer-directory set exceeds its {max_roots:,}-root work budget")
        stages["directory_roots_s"] = round(monotonic() - stage, 6)
        stage = monotonic()
        contributions = f"""
            SELECT l.pre, l.b, l.o, toUInt8(0) AS shard FROM (
                SELECT *, toUInt8(0) AS shard FROM ({source}) WHERE pre = post
            ) l ASOF LEFT JOIN (
                SELECT pre, post, toUInt8(0) AS shard, toUInt8(1) AS covered FROM {roots}
            ) r ON l.shard = r.shard AND l.pre >= r.pre
            WHERE r.covered = 0 OR l.pre > r.post
            UNION ALL SELECT pre, b, o, toUInt8(0) AS shard FROM {roots}
        """
        totals = ch.json(f"""
            SELECT c.pre, sum(s.b), sum(s.o) FROM ({contributions}) s
            ASOF INNER JOIN {bounds} c ON s.shard = c.shard AND s.pre >= c.pre
            WHERE s.pre <= c.post GROUP BY c.pre
        """, settings={"join_algorithm": "hash", "join_use_nulls": 0})
        stages["aggregate_s"] = round(monotonic() - stage, 6)
    rows = _complete(buckets, totals)
    body = {"schema": "hot-l1-v1", "target": target, "snapshot_db": db, "date": date, "pattern": pattern,
            "exact": True, "incremental": False, "scope": "case-insensitive substring within names; directory hits cover descendants; bytes/objects only",
            "root": {"b": sum(row["b"] for row in rows), "o": sum(row["o"] for row in rows)}, "buckets": rows,
            "staged_vocabulary_names": vocabulary_count, "matching_nonleaf_rows": nonleaf_count, "outer_directory_roots": root_count,
            "all_buckets_covered": shortcut, "stages": stages, "build_s": round(monotonic() - start, 6)}
    if any(cap is not None for cap in (max_names, max_postings, max_roots)):
        body.update(work_bounds={'max_names': max_names, 'max_postings': max_postings, 'max_outer_roots': max_roots},
                    direct_matching_rows=posting_count)
    return body


def oracle(ch: Ch, body: dict) -> bool:
    """Independent first-matching-path scan, including directory rollups.

    A slash-free literal is monotone along a root-to-leaf path. A matching
    node whose parent does not match is therefore on a disjoint frontier:
    its recursive weight includes its own object and all descendants once.
    This scans snapshot paths directly, never the name index or staged IDs.
    """
    target, db = identifier(body["target"]), identifier(body["snapshot_db"])
    pattern = body["pattern"]
    parent = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"
    source = f"""SELECT pre, b, o, toUInt8(0) AS shard FROM {db}.nodes
        WHERE lowerUTF8(path) LIKE {like_lit(pattern)} AND NOT (lowerUTF8({parent}) LIKE {like_lit(pattern)})"""
    buckets = _buckets(ch, target)
    totals = ch.json(f"""
        SELECT c.pre, sum(s.b), sum(s.o) FROM ({source}) s
        ASOF INNER JOIN ({_bounds_sql(buckets)}) c ON s.shard = c.shard AND s.pre >= c.pre
        WHERE s.pre <= c.post GROUP BY c.pre
    """, settings={"join_algorithm": "hash", "join_use_nulls": 0})
    rows = _complete(buckets, totals)
    expected = {"b": sum(row["b"] for row in rows), "o": sum(row["o"] for row in rows)}
    if rows != body["buckets"] or expected != body["root"]:
        raise AssertionError("hot L1 full-path frontier oracle disagrees with complete bucket totals")
    return True
