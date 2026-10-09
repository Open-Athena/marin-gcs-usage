"""Single-literal subtree coverage: exact bytes/objects, no path-count claim.

Matching directory intervals are atomic roots, not expanded into every file.
Intervals in a file tree are laminar. A queried directory either lies inside
one matching root (use its ordinary rollup), or contains entire matching
roots (sum their weights). Never double-count nested matching roots.
"""

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from hashlib import sha256
from json import loads
from itertools import accumulate
from time import monotonic
from sys import byteorder
from uuid import uuid4

from ..bench.ch import like_lit
from .ancestry import outer_roots_sql
from .client import Ch, lit
from .coarse import CoarseRequest, byte_ranks, select_rows
from .narrow import disk_reserve, identifier
from .range_bench import Prefix, aggregate, cumulative
from .serve import depth_of


@dataclass(frozen=True)
class Coverage:
    target: str
    date: str
    db: str
    pattern: str
    root: str
    starts: array
    ends: array
    prefix: Prefix
    build_s: float
    root_table: str
    name_table: str
    weights: tuple[array, array]
    resident_roots: bool = False
    block_rows: int = 4096
    root_plan: str = "interval"

    @property
    def source(self) -> str:
        return f"SELECT pre, post, b, o FROM {self.root_table}"

    def fingerprint(self) -> str:
        """Complete sorted root identities/weights, canonical little-endian."""
        digest = sha256(len(self.starts).to_bytes(8, "little"))
        for column in (self.starts, self.ends, *self.weights):
            if byteorder != "little":
                column = array("Q", column)
                column.byteswap()
            digest.update(column.tobytes())
        return digest.hexdigest()

    @classmethod
    def build(
        cls,
        ch: Ch,
        target: str,
        date: str,
        pattern: str,
        *,
        max_roots: int = 2_000_000,
        min_free_bytes: int = 20 << 30,
        resident_roots: bool = False,
        root_plan: str = "interval",
        max_names: int = 500_000,
    ) -> "Coverage":
        identifier(target)
        if root_plan not in ("interval", "ancestry"):
            raise CoarseRequest("coverage root plan must be interval or ancestry")
        if not pattern or "/" in pattern or len(pattern) > 512:
            raise CoarseRequest("one nonempty literal without slashes is required")
        if not 1 <= max_names <= 500_000:
            raise CoarseRequest("coverage vocabulary budget must be from 1 to 500K names")
        start = monotonic()
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        if date not in manifest["dates"]:
            raise CoarseRequest("scan outside the frozen index")
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        pattern = pattern.lower()
        tag = uuid4().hex
        names, candidates, roots = (f"coverage_{part}_{tag}" for part in ("names", "candidates", "roots"))
        ch.tmp(names, f"SELECT nid FROM {target}.names WHERE l LIKE {like_lit(pattern)} LIMIT {max_names + 1}")
        if int(ch.scalar(f"SELECT count() FROM {names}")) > max_names:
            raise CoarseRequest(f"coverage vocabulary exceeds its {max_names:,}-name work budget")
        source = f"SELECT pre, post, b, o FROM {db}.nodes_by_name WHERE nid IN (SELECT nid FROM {names})"
        count, nonleaf = ch.json(f"SELECT count(), countIf(pre != post) FROM ({source})")[0]
        if not nonleaf and count > max_roots:
            raise CoarseRequest(f"coverage leaf-only set exceeds its {max_roots:,}-root work budget; use a leaf mode")
        disk_reserve(ch, "coverage numeric roots", min_free_bytes)
        ch.tmp(candidates, source, disk=True, order_by="pre")
        if root_plan == "interval":
            root_source = f"""
            SELECT pre, post, b, o FROM (
                SELECT *, row_number() OVER (ORDER BY pre) AS rn,
                    max(post) OVER (ORDER BY pre ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_end
                FROM {candidates}
            ) WHERE rn = 1 OR pre > previous_end
            """
        else:
            parents, hierarchy = f"coverage_parents_{tag}", f"coverage_hierarchy_{tag}"
            ch.tmp(parents, f"""SELECT c.pre AS id, p.parent_pre AS parent_id, c.b, c.o FROM {candidates} c
                INNER JOIN (SELECT pre, parent_pre FROM {target}.numeric_parents WHERE pre IN (SELECT pre FROM {candidates})) p
                ON c.pre = p.pre""", disk=True, order_by="id")
            if int(ch.scalar(f"SELECT count() FROM {parents}")) != count:
                raise ValueError("coverage ancestry lost candidate parent identities")
            ch.tmp(hierarchy, f"SELECT pre AS id, ancestors FROM {target}.hierarchy WHERE pre IN (SELECT parent_id FROM {parents})")
            if int(ch.scalar(f"SELECT count() FROM (SELECT DISTINCT parent_id FROM {parents} WHERE parent_id >= 0) WHERE parent_id NOT IN (SELECT id FROM {hierarchy})")):
                raise ValueError("coverage ancestry is missing a selected parent hierarchy")
            selected = outer_roots_sql(target, parents, hierarchy, temporary=True, temporary_hierarchy=True)
            root_source = f"SELECT pre, post, b, o FROM {candidates} WHERE pre IN (SELECT id FROM ({selected}))"
        ch.tmp(roots, root_source, disk=True, order_by="pre")
        count = int(ch.scalar(f"SELECT count() FROM {roots}"))
        if count > max_roots:
            raise CoarseRequest(f"coverage outer-root locator exceeds its {max_roots:,}-root work budget")
        rows = ch.json(f"SELECT pre, post, b, o FROM {roots} ORDER BY pre")
        if any(a[1] >= b[0] for a, b in zip(rows, rows[1:])):
            raise RuntimeError("coverage roots are not disjoint")
        blocks = ch.json(f"SELECT intDiv(pre, 4096) AS block, count(), sum(b), sum(o) FROM {roots} GROUP BY block ORDER BY block")
        weights = tuple(array("Q", accumulate((row[i] for row in rows), initial=0)) for i in (2, 3))
        return cls(target, date, db, pattern, manifest["prefix"], array("Q", (r[0] for r in rows)),
                   array("Q", (r[1] for r in rows)), Prefix(blocks), monotonic() - start, roots, names,
                   weights, resident_roots, root_plan=root_plan)

    def resolve(
        self,
        ch: Ch,
        path: str,
        *,
        allow_absent: bool = False,
    ) -> tuple[int, int, int, int, bool]:
        if self.root and path != self.root and not path.startswith(self.root + "/"):
            raise CoarseRequest("path outside the frozen index")
        rows = ch.json(f"SELECT pre, post FROM {self.target}.dictionary WHERE depth = {depth_of(path)} AND path = {lit(path)}")
        if len(rows) != 1:
            raise CoarseRequest("path not in the frozen dictionary")
        lo, hi = rows[0]
        point = ch.json(f"SELECT b, o FROM {self.db}.nodes WHERE pre = {lo}")
        if not point and allow_absent:
            return lo, hi, 0, 0, False
        if len(point) != 1:
            raise CoarseRequest("path not present at the selected scan")
        return lo, hi, *point[0], True

    def bounds(self, ch: Ch, path: str) -> tuple[int, int, int, int]:
        return self.resolve(ch, path)[:4]

    def covers(self, lo: int, hi: int) -> bool:
        i = bisect_right(self.starts, lo) - 1
        return i >= 0 and hi <= self.ends[i]

    def totals(
        self,
        ch: Ch,
        ranges: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Inclusive subtree bounds, never arbitrary partial-tree ranges."""
        uncovered = [(lo, hi) for lo, hi in ranges if not self.covers(lo, hi)]
        if self.resident_roots:
            values = {bounds: tuple(weights[bisect_right(self.starts, bounds[1])] - weights[bisect_left(self.starts, bounds[0])]
                                    for weights in self.weights) for bounds in uncovered}
        else:
            values = {bounds: (value[1], value[2]) for bounds, value in zip(uncovered, aggregate(
                ch, self.source, [(lo, hi + 1) for lo, hi in uncovered], self.prefix, self.block_rows,
            ))}
        covered = [lo for lo, hi in ranges if self.covers(lo, hi)]
        points = dict((row[0], tuple(row[1:])) for row in ch.json(
            f"SELECT pre, b, o FROM {self.db}.nodes WHERE pre IN ({','.join(map(str, covered))})"
        )) if covered else {}
        return [points.get(lo, (0, 0)) if self.covers(lo, hi) else values[lo, hi] for lo, hi in ranges]

    def view(
        self,
        ch: Ch,
        path: str,
        budget: int = 64,
        *,
        threshold: int | None = None,
        allow_absent: bool = False,
    ) -> dict:
        start = monotonic()
        lo, hi, ordinary_b, ordinary_o, present = self.resolve(ch, path, allow_absent=allow_absent)
        covered = self.covers(lo, hi)
        b, o = (0, 0) if not present else (ordinary_b, ordinary_o) if covered else self.totals(ch, [(lo, hi)])[0]
        normal_threshold, _ = byte_ranks(b, budget)
        threshold = normal_threshold if threshold is None else threshold
        if threshold < normal_threshold:
            raise CoarseRequest("coverage threshold exceeds its quantile work budget")
        ranks = list(range(threshold, b + 1, threshold))
        candidates = []
        if lo != hi and b:
            if covered:
                # Parent-key/size order serves a complete bounded heavy set.
                rows = ch.json(f"SELECT pre, path FROM {self.db}.metadata_by_parent WHERE parent_pre = {lo} AND pre > {lo} AND b >= {threshold} LIMIT {budget + 1}")
                if len(rows) > budget:
                    raise RuntimeError("ordinary heavy children exceed their parent budget")
                paths = [row[1] for row in rows]
            else:
                before = self.weights[0][bisect_left(self.starts, lo)] if self.resident_roots else cumulative(ch, self.source, [lo], self.prefix, self.block_rows)[lo][1]
                ranks = [before + rank for rank in ranks]
                if self.resident_roots:
                    selected = sorted({self.starts[bisect_left(self.weights[0], rank) - 1] for rank in ranks})
                else:
                    blocks = sorted({self.prefix.blocks[bisect_left(self.prefix.totals[1], rank) - 1] for rank in ranks})
                    rows = ch.json(f"SELECT pre, b FROM {self.root_table} WHERE intDiv(pre, {self.block_rows}) IN ({','.join(map(str, blocks))}) ORDER BY pre")
                    selected = sorted(set(select_rows(self.prefix, ranks, rows, self.block_rows)))
                selected_paths = ch.json(f"SELECT path FROM {self.db}.nodes WHERE pre IN ({','.join(map(str, selected))})")
                prefix = path + "/" if path else ""
                paths = sorted({prefix + row[0][len(prefix):].split("/")[0] for row in selected_paths})
            if paths:
                candidates = ch.json(f"SELECT pre, post, path FROM {self.target}.dictionary WHERE depth = {depth_of(path) + 1} AND path IN ({','.join(map(lit, paths))})")
                if len(candidates) != len(paths) or any(not lo < a <= z <= hi for a, z, _ in candidates):
                    raise RuntimeError("coverage child dictionary intervals are inconsistent")
        totals = self.totals(ch, [(a, z) for a, z, _ in candidates])
        children = [{"pre": a, "path": p, "label": p.rsplit("/", 1)[-1], "b": cb, "o": co, "leaf": a == z}
                    for (a, z, p), (cb, co) in zip(candidates, totals) if cb >= threshold]
        children.sort(key=lambda child: (-child["b"], child["path"]))
        other = {"b": b - sum(c["b"] for c in children), "o": o - sum(c["o"] for c in children)}
        if lo == hi:
            other = {"b": 0, "o": 0}
        if any(value < 0 for value in other.values()):
            raise RuntimeError("coverage children exceed their exact parent totals")
        return {"schema": "coverage-v1", "date": self.date, "pattern": self.pattern, "path": path, "present": present,
                "exact": True, "incremental": False, "scope": "single literal full-path substring, bytes/objects only; path counts not computed",
                "threshold_bytes": threshold, "child_budget": budget, "covered_parent": covered,
                "tree": {"pre": lo, "path": path, "label": path.rsplit("/", 1)[-1] or "all buckets", "b": b, "o": o, "leaf": lo == hi, "children": children, "other": other},
                "response_s": round(monotonic() - start, 4)}


def oracle(
    ch: Ch,
    index: Coverage,
    body: dict,
    *,
    max_rows: int = 100_000,
) -> bool:
    """Independent full-path test and string-ancestor dedup, bounded for QA."""
    path, tree, threshold = body["path"], body["tree"], body["threshold_bytes"]
    lo, hi, ordinary_b, ordinary_o, present = index.resolve(ch, path, allow_absent=True)
    if body["present"] != present:
        raise ValueError("coverage scan presence differs from the selected snapshot")
    hit = index.pattern in path.lower()
    if not present:
        expected, children = (0, 0), {}
    elif hit:
        expected = ordinary_b, ordinary_o
        child_rows = ch.json(f"SELECT path, b, o FROM {index.db}.metadata_by_parent WHERE parent_pre = {lo} AND pre > {lo} AND b >= {threshold} LIMIT {max_rows + 1}") if lo != hi else []
        if len(child_rows) > max_rows:
            raise ValueError(f"coverage oracle child map exceeds its {max_rows:,}-row work budget")
        children = {row[0]: tuple(row[1:]) for row in child_rows}
    else:
        source = f"SELECT pre, path, b, o FROM {index.db}.nodes_by_name WHERE nid IN (SELECT nid FROM {index.name_table}) AND pre >= {lo} AND pre <= {hi}"
        if int(ch.scalar(f"SELECT count() FROM ({source})")) > max_rows:
            raise ValueError(f"coverage oracle matching-node set exceeds its {max_rows:,}-row work budget")
        rows = ch.json(source)
        rows.sort(key=lambda r: (depth_of(r[1]), r[1]))
        roots = {}
        for _, p, b, o in rows:
            segments = p.split("/")
            if not any("/".join(segments[:i]) in roots for i in range(1, len(segments))):
                roots[p] = b, o
        expected = sum(v[0] for v in roots.values()), sum(v[1] for v in roots.values())
        children = {}
        prefix = path + "/" if path else ""
        for p, values in roots.items():
            child = prefix + p[len(prefix):].split("/")[0]
            previous = children.get(child, (0, 0))
            children[child] = tuple(a + b for a, b in zip(previous, values))
        children = {p: v for p, v in children.items() if v[0] >= threshold} if lo != hi else {}
    if (tree["b"], tree["o"]) != expected or {c["path"]: (c["b"], c["o"]) for c in tree["children"]} != children:
        raise ValueError("coverage partition differs from independent full-path/string-ancestor aggregation")
    remainder = {key: expected[i] - sum(c[key] for c in tree["children"]) for i, key in enumerate(("b", "o"))}
    if tree["leaf"]:
        remainder = {"b": 0, "o": 0}
    if tree["other"] != remainder:
        raise ValueError("coverage folded remainder disagrees with its exact partition")
    return True


def diff(
    ch: Ch,
    before: Coverage,
    after: Coverage,
    path: str,
    budget: int = 64,
) -> dict:
    """Two exact partitions on the union of both dates' heavy children."""
    start = monotonic()
    if (before.target, before.pattern, before.root) != (after.target, after.pattern, after.root):
        raise CoarseRequest("coverage diff requires one dictionary and literal")
    if not 1 <= budget <= 128:
        raise CoarseRequest("coverage diff budget must be from 1 to 128")
    totals = []
    for index in (before, after):
        lo, hi, b, o, present = index.resolve(ch, path, allow_absent=True)
        totals.append((0, 0) if not present else (b, o) if index.covers(lo, hi) else index.totals(ch, [(lo, hi)])[0])
    threshold, _ = byte_ranks(max(total[0] for total in totals), budget)
    sides = [index.view(ch, path, budget, threshold=threshold, allow_absent=True) for index in (before, after)]
    paths = sorted({child["path"] for side in sides for child in side["tree"]["children"]})
    bounds = ch.json(f"SELECT pre, post, path FROM {before.target}.dictionary WHERE depth = {depth_of(path) + 1} AND path IN ({','.join(map(lit, paths))})") if paths else []
    if len(bounds) != len(paths) or len(paths) > 2 * budget:
        raise RuntimeError("coverage union violates dictionary or child work bounds")
    values = []
    for index, side in zip((before, after), sides):
        known = {c["path"]: (c["b"], c["o"]) for c in side["tree"]["children"]}
        missing = [(lo, hi, p) for lo, hi, p in bounds if p not in known]
        known.update({p: value for (_, _, p), value in zip(missing, index.totals(ch, [(lo, hi) for lo, hi, _ in missing]))})
        values.append(known)
    order = sorted(paths, key=lambda p: (-max(value[p][0] for value in values), p))
    geometry = {p: (lo, hi) for lo, hi, p in bounds}
    for side, known in zip(sides, values):
        tree = side["tree"]
        tree["children"] = [{"pre": geometry[p][0], "path": p, "label": p.rsplit("/", 1)[-1],
                             "b": known[p][0], "o": known[p][1], "leaf": geometry[p][0] == geometry[p][1]} for p in order]
        tree["other"] = {key: tree[key] - sum(child[key] for child in tree["children"]) for key in ("b", "o")}
        if tree["leaf"]:
            tree["other"] = {"b": 0, "o": 0}
        if any(value < 0 for value in tree["other"].values()):
            raise RuntimeError("coverage union children exceed their exact parent totals")
    return {"schema": "coverage-diff-v1", "before": sides[0], "after": sides[1], "exact": True, "incremental": False,
            "threshold_bytes": threshold, "max_children": 2 * budget,
            "delta": {key: totals[1][i] - totals[0][i] for i, key in enumerate(("b", "o"))},
            "response_s": round(monotonic() - start, 4)}


def diff_oracle(
    ch: Ch,
    before: Coverage,
    after: Coverage,
    body: dict,
    *,
    max_rows: int = 100_000,
) -> bool:
    """Check each complete heavy set, then all cross-side union weights."""
    union = {c["path"] for c in body["before"]["tree"]["children"]}
    if union != {c["path"] for c in body["after"]["tree"]["children"]}:
        raise ValueError("coverage diff partitions are not aligned")
    for index, side in zip((before, after), (body["before"], body["after"])):
        heavy = [c for c in side["tree"]["children"] if c["b"] >= body["threshold_bytes"]]
        tree = side["tree"]
        remainder = {key: tree[key] - sum(c[key] for c in tree["children"]) for key in ("b", "o")}
        if tree["leaf"]:
            remainder = {"b": 0, "o": 0}
        if tree["other"] != remainder:
            raise ValueError("coverage diff folded remainder disagrees with its exact partition")
        heavy_remainder = {key: tree[key] - sum(c[key] for c in heavy) for key in ("b", "o")} if not tree["leaf"] else {"b": 0, "o": 0}
        oracle(ch, index, {**side, "tree": {**tree, "children": heavy, "other": heavy_remainder}}, max_rows=max_rows)
        for child in side["tree"]["children"]:
            oracle(ch, index, {**side, "path": child["path"], "present": bool(ch.json(f"SELECT pre FROM {index.db}.nodes WHERE pre = {child['pre']}")),
                              "threshold_bytes": child["b"] + 1,
                              "tree": {**child, "children": [], "other": {key: 0 if child["leaf"] else child[key] for key in ("b", "o")}}}, max_rows=max_rows)
    expected = {key: body["after"]["tree"][key] - body["before"]["tree"][key] for key in ("b", "o")}
    if body["delta"] != expected:
        raise ValueError("coverage diff delta disagrees with its exact partitions")
    return True


def bench(
    url: str,
    target: str,
    date: str,
    pattern: str,
    *,
    budget: int = 64,
    cold: bool = False,
    compare: bool = False,
    paths: tuple[str, ...] = (),
    out: str | None = None,
    date0: str | None = None,
    resident_roots: bool = False,
    oracle_rows: int = 100_000,
    root_plan: str = "interval",
    cold_build: bool = False,
) -> dict:
    from json import dumps
    from pathlib import Path

    from .bench import drop_caches

    ch = Ch(url, db=identifier(target), max_threads=8, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_before_external_group_by=256 << 20,
            max_execution_time=120, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        if cold_build:
            drop_caches(url)
        index = Coverage.build(ch, target, date, pattern, resident_roots=resident_roots, root_plan=root_plan)
        if date0 and cold_build:
            drop_caches(url)
        before = Coverage.build(ch, target, date0, pattern, resident_roots=resident_roots, root_plan=root_plan) if date0 else None
        todo, views = list(paths) or [index.root], []
        while todo:
            path = todo.pop(0)
            if cold:
                drop_caches(url)
            body = diff(ch, before, index, path, budget) if before else index.view(ch, path, budget)
            current = body["after"] if before else body
            row = {"path": path, "response_s": body["response_s"], "covered_parent": current["covered_parent"],
                   "children": len(current["tree"]["children"]), "verified": False}
            if compare:
                if cold:
                    drop_caches(url)
                start = monotonic()
                row["verified"] = diff_oracle(ch, before, index, body, max_rows=oracle_rows) if before else oracle(ch, index, body, max_rows=oracle_rows)
                row["oracle_s"] = round(monotonic() - start, 4)
            if out:
                directory = Path(out)
                directory.mkdir(parents=True, exist_ok=True)
                artifact = directory / f"view-{len(views)}.json"
                artifact.write_text(dumps(body, indent=2) + "\n")
                row["body_file"] = str(artifact)
            views.append(row)
            if not paths and len(views) == 1:
                todo.extend(c["path"] for c in current["tree"]["children"] if not c["leaf"])
                todo = todo[:2]
        return {"date": date, "pattern": pattern, "target": target, "cold": cold, "threads": 8,
                "scope": "exact single-literal subtree bytes/objects; no path counts or serving changes",
                "outer_roots": len(index.starts), "summary_bytes": len(index.prefix.blocks) * 32,
                "build_s": round(index.build_s + (before.build_s if before else 0), 4), "views": views,
                "date0": date0, "outer_roots0": len(before.starts) if before else None,
                "resident_roots": resident_roots, "root_plan": root_plan,
                "cold_build": cold_build, "root_fingerprint": index.fingerprint(),
                "root_fingerprint0": before.fingerprint() if before else None,
                "packed_locator_bytes": (32 * len(index.starts) + 16) + (32 * len(before.starts) + 16 if before else 0)}
    finally:
        ch.close()
