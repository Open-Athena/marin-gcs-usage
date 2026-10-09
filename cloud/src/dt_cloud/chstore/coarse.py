"""Exact, bounded one-level treemaps over frozen leaf-name range summaries.

A child with at least T bytes contains a rank on the T-spaced byte grid.
Select those ranks, map each selected leaf to its immediate child, then get
that small candidate set's exact range totals. No enumeration of all children
or all matching leaves is needed. Omitted children form an exact remainder.
"""

from bisect import bisect_left
from array import array
from collections import defaultdict
from dataclasses import dataclass
from json import loads
from pathlib import Path
from time import monotonic

from .client import Ch, lit
from .narrow import disk_reserve, identifier
from .range_bench import NonLeafMatches, Prefix, aggregate, build_prefix, cumulative
from .serve import depth_of


class CoarseRequest(ValueError):
    """A requested exact name, path or scan is outside this prototype."""


def byte_ranks(total: int, budget: int) -> tuple[int, list[int]]:
    if total < 0 or not 1 <= budget <= 256:
        raise ValueError("nonnegative bytes and a 1..256 child budget are required")
    threshold = max(1, (total + budget - 1) // budget)
    return threshold, list(range(threshold, total + 1, threshold))


def select_rows(
    prefix: Prefix,
    ranks: list[int],
    rows: list[list[int]],
    block_rows: int,
) -> list[int]:
    """Select 1-based global byte ranks from bounded boundary-block reads."""
    groups = defaultdict(list)
    for pre, b in rows:
        groups[pre // block_rows].append((pre, b))
    local = {}
    for block, entries in groups.items():
        before = prefix.before(block)[1]
        positions, totals = [], []
        for pre, b in entries:
            before += b
            positions.append(pre)
            totals.append(before)
        local[block] = positions, totals
    selected = []
    for rank in ranks:
        if not 1 <= rank <= prefix.totals[1][-1]:
            raise ValueError("byte rank outside the indexed postings")
        block = prefix.blocks[bisect_left(prefix.totals[1], rank) - 1]
        positions, totals = local[block]
        selected.append(positions[bisect_left(totals, rank)])
    return selected


@dataclass(frozen=True)
class NameIndex:
    target: str
    date: str
    db: str
    nid: int
    name: str
    root: str
    prefix: Prefix
    block_rows: int
    build_s: float
    source_override: str | None = None
    predicate_mode: str = "exact"
    vocabulary_names: int = 1
    oracle_source_override: str | None = None
    build_stages: dict | None = None
    name_ids: array | None = None
    name_table: str | None = None
    prepared_set: bool = False

    @property
    def source(self) -> str:
        if self.source_override is not None:
            return self.source_override
        return f"SELECT pre, post, b, o FROM {self.db}.nodes_by_name WHERE nid = {self.nid}"

    @classmethod
    def build(
        cls,
        ch: Ch,
        target: str,
        date: str,
        name: str,
        block_rows: int = 4096,
    ) -> "NameIndex":
        identifier(target)
        if block_rows < 1024:
            raise ValueError("block size must be at least 1024")
        start = monotonic()
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        if date not in manifest["dates"]:
            raise CoarseRequest("scan outside the frozen index")
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        name = name.lower()
        nid = ch.scalar(f"SELECT nid FROM {target}.names WHERE l = {lit(name)}")
        if nid is None:
            raise CoarseRequest(f"unknown exact name: {name}")
        source = f"SELECT pre, post, b, o FROM {db}.nodes_by_name WHERE nid = {int(nid)}"
        prefix = build_prefix(ch, source, block_rows)
        return cls(target, date, db, int(nid), name, manifest["prefix"], prefix, block_rows, monotonic() - start)

    @classmethod
    def build_pattern(
        cls,
        ch: Ch,
        target: str,
        date: str,
        suffix: str,
        *,
        materialize: bool = False,
        match_mode: str = "suffix",
        block_rows: int = 4096,
        min_free_bytes: int = 20 << 30,
        max_names: int = 500_000,
        prepared_set: bool = False,
    ) -> "NameIndex":
        """Leaf category; reusable packed vocabulary, optional benchmark cache."""
        identifier(target)
        if not suffix or "/" in suffix or len(suffix) > 512 or block_rows < 1024 or match_mode not in ("suffix", "contains"):
            raise CoarseRequest("a basename suffix and block size >=1024 are required")
        if not 1 <= max_names <= 5_000_000:
            raise CoarseRequest("pattern vocabulary budget must be from 1 to 5M names")
        start = monotonic()
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        if date not in manifest["dates"]:
            raise CoarseRequest("scan outside the frozen index")
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        suffix = suffix.lower()
        stages, stage_start = {}, monotonic()
        # Different names have disjoint postings. No n-gram overlap counting.
        names = f"coarse_suffix_names_{manifest['dates'].index(date)}"
        from ..bench.ch import like_lit

        condition = f"endsWith(l, {lit(suffix)})" if match_mode == "suffix" else f"l LIKE {like_lit(suffix)}"
        ch.tmp(names, f"SELECT nid FROM {target}.names WHERE {condition} LIMIT {max_names + 1}")
        n_names = int(ch.scalar(f"SELECT count() FROM {names}"))
        if n_names > max_names:
            raise CoarseRequest(f"pattern vocabulary exceeds the {max_names:,}-name work budget")
        name_ids = array("I", (row[0] for row in ch.json(f"SELECT nid FROM {names}")))
        stages["vocabulary_s"] = round(monotonic() - stage_start, 4)
        stage_start = monotonic()
        source = f"SELECT pre, post, b, o FROM {db}.nodes_by_name WHERE nid IN (SELECT nid FROM {names})"
        # Validate before copying any data or using sums as an exact leaf index.
        prefix = build_prefix(ch, source, block_rows)
        stages["summary_s"] = round(monotonic() - stage_start, 4)
        original_source = source
        if prepared_set:
            ch.tmp(names + "_set", f"SELECT nid FROM {names}", set_index=True)
            source = f"SELECT pre, post, b, o FROM {db}.nodes_by_name WHERE nid IN {names}_set"
        if materialize:
            stage_start = monotonic()
            disk_reserve(ch, "coarse suffix session cache", min_free_bytes)
            table = f"coarse_suffix_postings_{manifest['dates'].index(date)}"
            ch.tmp(table, source, disk=True, order_by="pre")
            source = f"SELECT pre, post, b, o FROM {table}"
            stages["postings_cache_s"] = round(monotonic() - stage_start, 4)
        return cls(target, date, db, 0, suffix, manifest["prefix"], prefix, block_rows, monotonic() - start,
                   source, match_mode, n_names, original_source, stages, name_ids, names, prepared_set)

    def prepare(self, ch: Ch) -> None:
        """Bind a cached pattern's packed vocabulary to a new query session."""
        from sys import byteorder

        if self.name_ids is None:
            return
        if self.name_table is None or self.name_ids.itemsize != 4:
            raise RuntimeError("cached vocabulary has no valid UInt32 binding")
        values = self.name_ids
        if byteorder != "little":
            values = array("I", values)
            values.byteswap()
        ch.tmp(self.name_table, "SELECT toUInt32(0) AS nid WHERE 0")
        if values:
            ch.insert(f"INSERT INTO {self.name_table} FORMAT RowBinary", values.tobytes())
        if self.prepared_set:
            ch.tmp(self.name_table + "_set", f"SELECT nid FROM {self.name_table}", set_index=True)

    def view(
        self,
        ch: Ch,
        path: str,
        budget: int = 64,
        *,
        allow_absent: bool = False,
    ) -> dict:
        start = monotonic()
        if self.root and path != self.root and not path.startswith(self.root + "/"):
            raise CoarseRequest("path outside the frozen index")
        parent = ch.json(f"SELECT pre, post FROM {self.target}.dictionary WHERE depth = {depth_of(path)} AND path = {lit(path)}")
        if len(parent) != 1:
            raise CoarseRequest("path not in the frozen dictionary")
        pre, post = parent[0]
        present = bool(ch.scalar(f"SELECT 1 FROM {self.db}.nodes WHERE pre = {pre} LIMIT 1"))
        if not present and not allow_absent:
            raise CoarseRequest("path not present at the selected scan")
        limits = cumulative(ch, self.source, [pre, post + 1], self.prefix, self.block_rows) if present else {pre: (0, 0, 0), post + 1: (0, 0, 0)}
        n, b, o = (end - begin for begin, end in zip(limits[pre], limits[post + 1]))
        threshold, ranks = byte_ranks(b, budget)
        candidates, selected = [], []
        if ranks and pre != post:
            ranks = [limits[pre][1] + rank for rank in ranks]
            blocks = sorted({self.prefix.blocks[bisect_left(self.prefix.totals[1], rank) - 1] for rank in ranks})
            postings = ch.json(f"SELECT pre, b FROM ({self.source}) WHERE intDiv(pre, {self.block_rows}) IN ({','.join(map(str, blocks))}) ORDER BY pre")
            selected = sorted(set(select_rows(self.prefix, ranks, postings, self.block_rows)))
            paths = ch.json(f"SELECT path FROM {self.db}.nodes WHERE pre IN ({','.join(map(str, selected))})")
            children = sorted({(path + "/" if path else "") + p[0][len(path) + (1 if path else 0):].split("/")[0] for p in paths})
            candidates = ch.json(f"SELECT pre, post, path FROM {self.target}.dictionary WHERE depth = {depth_of(path) + 1} AND path IN ({','.join(map(lit, children))}) ORDER BY pre")
            if len(candidates) != len(children) or any(not pre < lo <= hi <= post for lo, hi, _ in candidates):
                raise RuntimeError("selected child dictionary intervals are inconsistent")
        totals = aggregate(ch, self.source, [(lo, hi + 1) for lo, hi, _ in candidates], self.prefix, self.block_rows)
        kept = [{"pre": lo, "path": p, "label": p.rsplit("/", 1)[-1], "b": cb, "o": co, "matches": cn, "leaf": lo == hi}
                for (lo, hi, p), (cn, cb, co) in zip(candidates, totals) if cb >= threshold]
        kept.sort(key=lambda child: (-child["b"], child["path"]))
        remainder = {"b": b - sum(child["b"] for child in kept), "o": o - sum(child["o"] for child in kept),
                     "matches": n - sum(child["matches"] for child in kept)}
        if pre == post:
            remainder = {"b": 0, "o": 0, "matches": 0}
        if any(value < 0 for value in remainder.values()):
            raise RuntimeError("coarse children exceed the exact parent totals")
        return {
            "schema": "coarse-v1", "date": self.date, "name": self.name, "mode": self.predicate_mode, "path": path, "exact": True, "present": present,
            "scope": f"leaf basename {self.predicate_mode}, bytes/counts only", "incremental": False,
            "tree": {"pre": pre, "path": path, "label": path.rsplit("/", 1)[-1] or "all buckets", "b": b, "o": o,
                     "matches": n, "leaf": pre == post, "children": kept, "other": remainder},
            "threshold_bytes": threshold, "child_budget": budget, "quantile_points": len(selected),
            "candidate_children": len(candidates), "response_s": round(monotonic() - start, 4),
        }


def diff(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    path: str,
    budget: int = 64,
) -> dict:
    """Exact two-date partition, selected by max side bytes, never net delta.

    Union the sides' heavy children, then look up every union child on both
    dates. With T = ceil(max(parent bytes)/K), every child >= T on either
    side is discovered. At most 2K children survive; cancellation cannot hide
    a large directory. The remainder and its signed delta stay exact.
    """
    if before.target != after.target or before.name != after.name or before.root != after.root or before.predicate_mode != after.predicate_mode:
        raise ValueError("coarse diff requires one frozen dictionary and predicate")
    if not 1 <= budget <= 128:
        raise CoarseRequest("diff budget must be from 1 to 128 (at most twice that many children)")
    start = monotonic()
    sides = [index.view(ch, path, budget, allow_absent=True) for index in (before, after)]
    if not any(side["present"] for side in sides):
        raise CoarseRequest("path not present at either selected scan")
    threshold, _ = byte_ranks(max(side["tree"]["b"] for side in sides), budget)
    children = sorted({c["path"] for side in sides for c in side["tree"]["children"]})
    candidates = ch.json(f"SELECT pre, post, path FROM {before.target}.dictionary WHERE depth = {depth_of(path) + 1} AND path IN ({','.join(map(lit, children))}) ORDER BY pre") if children else []
    if len(candidates) != len(children):
        raise RuntimeError("diff child dictionary intervals are inconsistent")
    ranges = [(lo, hi + 1) for lo, hi, _ in candidates]
    totals = [aggregate(ch, index.source, ranges, index.prefix, index.block_rows) for index in (before, after)]
    kept = [i for i in range(len(candidates)) if max(totals[0][i][1], totals[1][i][1]) >= threshold]
    kept.sort(key=lambda i: (-max(totals[0][i][1], totals[1][i][1]), candidates[i][2]))
    for side, values in zip(sides, totals):
        tree = side["tree"]
        tree["children"] = [{"pre": candidates[i][0], "path": candidates[i][2], "label": candidates[i][2].rsplit("/", 1)[-1],
                             "b": values[i][1], "o": values[i][2], "matches": values[i][0], "leaf": candidates[i][0] == candidates[i][1]}
                            for i in kept]
        tree["other"] = {key: tree[key] - sum(c[key] for c in tree["children"]) for key in ("b", "o", "matches")}
        if tree["leaf"]:
            tree["other"] = {"b": 0, "o": 0, "matches": 0}
        if any(value < 0 for value in tree["other"].values()):
            raise RuntimeError("diff children exceed the exact parent totals")
        side["threshold_bytes"] = threshold
    return {"schema": "coarse-diff-v1", "exact": True, "incremental": False, "path": path, "name": after.name,
            "before": sides[0], "after": sides[1], "child_budget": budget, "max_children": 2 * budget,
            "threshold_bytes": threshold, "delta": {key: sides[1]["tree"][key] - sides[0]["tree"][key] for key in ("b", "o", "matches")},
            "response_s": round(monotonic() - start, 4)}


def oracle_rows(ch: Ch, index: NameIndex, body: dict) -> dict[int, tuple[int, int, int]]:
    """Independent complete immediate-child aggregation, never sampled.

    This is a validation-only full posting scan. ASOF assigns a leaf to its
    unique immediate child's interval. It does not use quantile candidates or
    prefix summaries, and bounds directory-map construction at 100K rows.
    """
    path = body["path"]
    pre, post = ch.json(f"SELECT pre, post FROM {index.target}.dictionary WHERE depth = {depth_of(path)} AND path = {lit(path)}")[0]
    source = index.oracle_source_override or index.source
    expected = ch.json(f"SELECT count(), sum(b), sum(o) FROM ({source}) WHERE pre >= {pre} AND pre <= {post}")[0]
    tree = body["tree"]
    if tuple(expected) != (tree["matches"], tree["b"], tree["o"]):
        raise ValueError("coarse parent totals differ from the independent posting scan")
    if pre == post:
        return {}
    children = f"SELECT pre, post FROM {index.target}.dictionary WHERE depth = {depth_of(path) + 1} AND pre > {pre} AND pre <= {post}"
    if int(ch.scalar(f"SELECT count() FROM ({children})")) > 100_000:
        raise ValueError("validation child map exceeds the bounded oracle budget")
    ch.tmp("coarse_oracle_children", f"SELECT *, toUInt8(0) AS shard FROM ({children}) ORDER BY pre")
    rows = ch.json(f"""
        SELECT c.pre, count() AS n, sum(s.b) AS b, sum(s.o) AS o
        FROM (SELECT *, toUInt8(0) AS shard FROM ({source}) WHERE pre > {pre} AND pre <= {post}) s
        ASOF INNER JOIN coarse_oracle_children c ON s.shard = c.shard AND s.pre >= c.pre
        WHERE s.pre <= c.post GROUP BY c.pre
    """, settings={"join_algorithm": "hash"})
    return {row[0]: tuple(row[1:]) for row in rows}


def oracle(ch: Ch, index: NameIndex, body: dict) -> bool:
    rows = oracle_rows(ch, index, body)
    tree = body["tree"]
    actual = sorted((child["pre"], child["matches"], child["b"], child["o"]) for child in tree["children"])
    expected = sorted((pre, *totals) for pre, totals in rows.items() if totals[1] >= body["threshold_bytes"])
    if actual != expected:
        raise ValueError("coarse visible children differ from complete independent aggregation")
    return True


def diff_oracle(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    body: dict,
) -> bool:
    rows = [oracle_rows(ch, index, body[key]) for index, key in ((before, "before"), (after, "after"))]
    heavy = sorted(pre for pre in set(rows[0]) | set(rows[1])
                   if max(r.get(pre, (0, 0, 0))[1] for r in rows) >= body["threshold_bytes"])
    for values, key in zip(rows, ("before", "after")):
        expected = [(pre, *values.get(pre, (0, 0, 0))) for pre in heavy]
        actual = sorted((c["pre"], c["matches"], c["b"], c["o"]) for c in body[key]["tree"]["children"])
        if actual != expected:
            raise ValueError("coarse diff children differ from complete independent aggregation")
    return True


def bench(
    url: str,
    target: str,
    date: str,
    name: str,
    *,
    budget: int = 64,
    cold: bool = False,
    compare: bool = False,
    paths: tuple[str, ...] = (),
    out: Path | None = None,
    threads: int = 8,
    date0: str | None = None,
    suffix: bool = False,
    contains: bool = False,
    materialize: bool = False,
    levels: int = 1,
    prepared_set: bool = False,
) -> dict:
    """One frozen index, root plus two drills unless explicit paths supplied."""
    from json import dumps

    from .bench import drop_caches

    ch = Ch(url, db=identifier(target), max_threads=threads, max_memory_usage=8 << 30,
            max_bytes_before_external_group_by=256 << 20, max_execution_time=120,
            timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
            max_bytes_before_external_sort=256 << 20, max_bytes_in_set=256 << 20, set_overflow_mode="throw")
    try:
        if suffix and contains:
            raise ValueError("suffix and contains are mutually exclusive")
        if prepared_set and not (suffix or contains):
            raise ValueError("prepared vocabulary Set requires a pattern mode")
        if materialize and not (suffix or contains):
            raise ValueError("session materialization is currently a pattern experiment only")
        def build(scan: str) -> NameIndex:
            return NameIndex.build_pattern(ch, target, scan, name, materialize=materialize, match_mode="suffix" if suffix else "contains", prepared_set=prepared_set) if suffix or contains else NameIndex.build(ch, target, scan, name)
        try:
            index = build(date)
        except NonLeafMatches as e:
            return {"target": target, "date": date, "name": name.lower(), "predicate_mode": "suffix" if suffix else "contains" if contains else "exact",
                    "status": "unsupported nonleaf matches", "posting_rows": e.posting_rows, "nonleaf_rows": e.nonleaf_rows,
                    "leaf_rows": e.posting_rows - e.nonleaf_rows, "serving_changed": False}
        before = build(date0) if date0 else None
        todo, results = list(paths) or [index.root], []
        while todo:
            path = todo.pop(0)
            if cold:
                drop_caches(url)
            from .coarse_walk import walk, walk_diff, walk_diff_oracle, walk_oracle

            if before:
                body = walk_diff(ch, before, index, path, budget, levels) if levels > 1 else diff(ch, before, index, path, budget)
            else:
                body = walk(ch, index, path, budget, levels) if levels > 1 else index.view(ch, path, budget)
            view = body["after"] if before else body
            row = {"path": path, "response_s": body["response_s"], "threshold_children": len(view["tree"]["children"]),
                   "quantile_points": view.get("quantile_points"), "candidate_children": view.get("candidate_children"),
                   "tree_nodes": body.get("tree_nodes"), "frontier_batches": body.get("frontier_batches"),
                   "exact_totals": True, "verified": False}
            if compare:
                if cold:
                    drop_caches(url)
                start = monotonic()
                if levels > 1:
                    row["verified_partitions"] = walk_diff_oracle(ch, before, index, body) if before else walk_oracle(ch, index, body)
                    row["verified"] = True
                else:
                    row["verified"] = diff_oracle(ch, before, index, body) if before else oracle(ch, index, body)
                row["oracle_s"] = round(monotonic() - start, 4)
            if out is not None:
                out.mkdir(parents=True, exist_ok=True)
                artifact = out / f"view-{len(results)}.json"
                artifact.write_text(dumps(body, indent=2) + "\n")
                row["body_file"] = str(artifact)
            results.append(row)
            if not paths and len(results) == 1:
                todo.extend(child["path"] for child in view["tree"]["children"] if not child["leaf"])
                todo = todo[:2]
        return {"target": target, "date": date, "date0": date0, "name": name.lower(), "predicate_mode": index.predicate_mode,
                "vocabulary_names": index.vocabulary_names, "session_materialized": materialize, "scope": "exact coarse byte/count treemap with drill-down",
                "prepared_set": prepared_set,
                "build_stages": index.build_stages,
                "nonleaf_rows": 0, "vocabulary_numeric_bytes": len(index.name_ids) * 4 if index.name_ids is not None else 0,
                "threads": threads, "cold": cold, "child_budget": budget, "levels": levels, "prefix_resident": True,
                "build_s": round(index.build_s + (before.build_s if before else 0), 4), "summary_numeric_bytes": (len(index.prefix.blocks) + (len(before.prefix.blocks) if before else 0)) * 32,
                "posting_rows": index.prefix.totals[0][-1], "posting_rows_before": before.prefix.totals[0][-1] if before else None,
                "views": results, "incremental": False,
                "rich_metrics": False}
    finally:
        ch.close()
