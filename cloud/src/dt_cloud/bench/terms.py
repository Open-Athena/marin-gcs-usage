"""How a query's matchers decompose over a tree of path segments: what the
serving-box engines (`mem`, `duck`) need on top of `query`'s full-path
predicate (specs/filter-query-service.md §4.1).

A substring / glob matcher is monotone: once it holds on a path it holds on
every extension. It *becomes* true at the node where its occurrence ends, and
an occurrence holding `k` slashes spans exactly that node's last `k + 1`
segments. So, exactly:

    holds(m, N) ⇔ ∃ A ∈ ancestors-or-self(N): m holds on suffix_{k+1}(A)

and the vocabulary prefilter — a necessary condition on A's own (lowercase)
name — bounds the A's to look at. A term ending in `/` (`tomat/`) holds
*strictly below* a node whose suffix ends in the stem (`strict`).

A regex matcher is not monotone (`\\.json$`); `regex_name_filter` derives a
necessary condition on the last segment's name when the regex's tail can't
cross a `/` and is anchored at the end, so its matches are found from the
vocabulary too. Otherwise the regex is unplannable (a full scan).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .query import Matcher, glob_re

try:  # 3.11+
    from re import _constants as C, _parser as P
except ImportError:  # pragma: no cover
    import sre_constants as C  # type: ignore[no-redef]
    import sre_parse as P  # type: ignore[no-redef]


@dataclass(frozen=True)
class NameTest:
    """A test on a lowercase segment name: `contains` / `starts` / `ends` /
    `equals` a literal, or an RE2 `regex` (searched)."""

    op: str
    arg: str

    def sql(self, col: str = "l") -> str:
        from .query import lit

        if self.op == "contains":
            return f"contains({col}, {lit(self.arg)})"
        if self.op == "starts":
            return f"starts_with({col}, {lit(self.arg)})"
        if self.op == "ends":
            return f"ends_with({col}, {lit(self.arg)})"
        if self.op == "equals":
            return f"{col} = {lit(self.arg)}"
        return f"regexp_matches({col}, {lit(self.arg)})"

    def test(self, name: str) -> bool:
        if self.op == "contains":
            return self.arg in name
        if self.op == "starts":
            return name.startswith(self.arg)
        if self.op == "ends":
            return name.endswith(self.arg)
        if self.op == "equals":
            return name == self.arg
        return re.search(self.arg, name) is not None


def name_test(pieces: list[str], start: bool, end: bool) -> NameTest:
    """Glob pieces (joined by `[^/]*`) within one segment, optionally
    anchored at its start / end."""
    if len(pieces) == 1:
        s = pieces[0]
        return NameTest({(False, False): "contains", (True, False): "starts", (False, True): "ends", (True, True): "equals"}[(start, end)], s)
    return NameTest("regex", ("^" if start else "") + glob_re(pieces) + ("$" if end else ""))


@dataclass(frozen=True)
class SegTerm:
    """A substring / glob matcher over segments. `suffix_re` is the term
    over a node's last `k + 1` segments joined by `/` (RE2, lowercase;
    ends in `$` when `strict`); `name` the necessary condition on the last
    segment; `trivial` when that condition passes every name."""

    k: int
    strict: bool
    name: NameTest
    suffix_re: str
    trivial: bool


def seg_term(m: Matcher) -> SegTerm:
    if m.kind == "regex":
        raise ValueError("a regex matcher has no segment decomposition (`regex_name_filter`)")
    pieces = [m.text] if m.kind == "sub" else list(m.pieces)
    strict = pieces[-1].endswith("/")
    if strict:
        pieces[-1] = pieces[-1][:-1]
    k = sum(p.count("/") for p in pieces)
    suffix_re = glob_re(pieces) + ("$" if strict else "")
    j = max((i for i, p in enumerate(pieces) if "/" in p), default=None)
    tail = pieces if j is None else [pieces[j].rsplit("/", 1)[1], *pieces[j + 1 :]]
    nt = name_test(tail, j is not None, strict)
    trivial = not "".join(tail)
    return SegTerm(k, strict, nt, suffix_re, trivial)


# --- regexes -------------------------------------------------------------------------

SLASH = ord("/")


def _cat_has_slash(cat) -> bool:
    # '/' is not a digit, word char or space; the negated categories hold it.
    return cat in (C.CATEGORY_NOT_DIGIT, C.CATEGORY_NOT_WORD, C.CATEGORY_NOT_SPACE, C.CATEGORY_UNI_NOT_DIGIT, C.CATEGORY_UNI_NOT_WORD, C.CATEGORY_UNI_NOT_SPACE)


def _in_has_slash(items) -> bool:
    neg = bool(items) and items[0][0] is C.NEGATE
    hit = False
    for op, av in items[1:] if neg else items:
        if op is C.LITERAL and av == SLASH:
            hit = True
        elif op is C.RANGE and av[0] <= SLASH <= av[1]:
            hit = True
        elif op is C.CATEGORY and _cat_has_slash(av):
            hit = True
    return hit != neg


def can_match_slash(items) -> bool:
    """Whether a parsed pattern can consume a `/` (conservative: unknown
    constructs say yes)."""
    for op, av in items:
        if op is C.LITERAL:
            if av == SLASH:
                return True
        elif op is C.NOT_LITERAL:
            if av != SLASH:
                return True
        elif op is C.ANY:
            return True
        elif op is C.IN:
            if _in_has_slash(av):
                return True
        elif op in (C.MAX_REPEAT, C.MIN_REPEAT, getattr(C, "POSSESSIVE_REPEAT", None)):
            if can_match_slash(av[2]):
                return True
        elif op is C.SUBPATTERN:
            if can_match_slash(av[-1]):
                return True
        elif op is getattr(C, "ATOMIC_GROUP", None):
            if can_match_slash(av):
                return True
        elif op is C.BRANCH:
            if any(can_match_slash(s) for s in av[1]):
                return True
        elif op is C.AT:
            continue
        else:
            return True
    return False


def _top_level_slashes(source: str) -> list[int] | None:
    """Positions of the literal `/`s at the pattern's top level (outside
    groups and classes); None when it has a top-level `|`."""
    out, depth, cls, i = [], 0, False, 0
    while i < len(source):
        c = source[i]
        if c == "\\":
            i += 2
            continue
        if cls:
            if c == "]":
                cls = False
        elif c == "[":
            cls = True
            if source[i + 1 : i + 2] == "^":
                i += 1
            if source[i + 1 : i + 2] == "]":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return None
        elif c == "/" and depth == 0:
            out.append(i)
        i += 1
    return out


@dataclass(frozen=True)
class RegexPlan:
    """A full-path regex's necessary condition on the matched node's name:
    `name_re` (RE2, searched case-insensitively)."""

    source: str
    name_re: str


def regex_name_filter(source: str) -> RegexPlan | None:
    """When every match of `source` (case-insensitive, searched in the full
    path) ends at the path's end and its part after the last top-level `/`
    can't consume a `/`, the matched node's name must match that tail
    (anchored at the name's start when a `/` precedes it). None otherwise."""
    slashes = _top_level_slashes(source)
    if slashes is None:
        return None
    cut = slashes[-1] + 1 if slashes else 0
    tail = source[cut:]
    try:
        items = list(P.parse(tail, re.IGNORECASE))
    except re.error:
        return None
    if not items or items[-1] != (C.AT, C.AT_END) and items[-1] != (C.AT, C.AT_END_STRING):
        return None
    if can_match_slash(items):
        return None
    if not tail.rstrip("$"):
        return None
    return RegexPlan(source, ("^" if slashes else "") + tail)
