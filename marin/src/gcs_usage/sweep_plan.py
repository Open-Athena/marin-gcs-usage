"""Object-level fate resolution for the sweep executor (specs/sweep-executor.md).

Python port of the site's ledger semantics, kept deliberately tiny and pure so
it can be property-tested against `/api/resolve` and reconciled against
`/api/marks/totals` before anything is allowed to delete:

- **Effective fate of a prefix** — the most-recent live keep row on an
  ancestor-or-equal prefix wins (`ts DESC, action_id DESC`); a NULL keep on the
  winner is an explicit unmark. Mirrors `site/functions/api/resolve.ts`.
- **Fate of an object key** — the effective fate of its *deepest marked
  ancestor*: the object's covering rows are exactly that ancestor's covering
  rows, so the winner is the same. Unmarked if no ancestor is in the ledger.
- **KLC expansion** — mirrors `site/src/sweep.ts` `klcSplits`, at object level:
  walking down from a `keep_last_ckpt` prefix, at the first level with
  step-numbered children the max-step child's subtree is kept and everything
  else at that node sweeps (no deeper recursion); levels without steps recurse.
  A band where the walk finds no steps at all is *unresolved* — the UI renders
  those amber, and the planner keeps them (flagged) rather than guessing.

Prefixes are the ledger's `gs://marin-<bucket>/<dir>/` form; object keys are
listing-style `<bucket>/<name>` (no scheme).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# Keep in lockstep with site/src/sweep.ts CKPT_NUM_RE and _lib/marks.ts.
CKPT_NUM_RE = re.compile(r"^(?:step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?(\d+)", re.I)

_GS_RE = re.compile(r"^gs://([a-z0-9-]+)/(.*)$")


@dataclass(frozen=True)
class KeepRow:
    """One live `keep_prefixes` row (from `GET /api/actions` `keeps`)."""

    prefix: str  # gs://marin-<bucket>/<dir>/ (trailing slash)
    keep: Optional[str]  # keep | keep_last_ckpt | sweep | None (explicit unmark)
    ts: int
    action_id: int
    who: str = ""
    memo: Optional[str] = None


def load_keeps(actions_payload: dict) -> list[KeepRow]:
    """`GET /api/actions` response → KeepRows."""
    return [
        KeepRow(
            prefix=r["prefix"],
            keep=r.get("keep"),
            ts=int(r["ts"]),
            action_id=int(r["action_id"]),
            who=r.get("who") or "",
            memo=r.get("memo"),
        )
        for r in actions_payload["keeps"]
    ]


def key_to_prefixes(bucket: str, name: str) -> list[str]:
    """Ancestor prefixes of one object key, bucket root first — the ledger
    prefixes that could cover it. Mirrors `_lib/resolve.ts ancestorPrefixes`."""
    root = f"gs://{bucket}/"
    out = [root]
    acc = root
    for seg in name.split("/")[:-1]:  # the last segment is the object basename
        acc += seg + "/"
        out.append(acc)
    return out


class FateResolver:
    """Ledger snapshot → effective fates, for prefixes and object keys."""

    def __init__(self, rows: Iterable[KeepRow]):
        # Winner per exact prefix first: the overall winner over a covering set
        # is the max of per-prefix maxima, so only each prefix's newest row
        # matters. Order rows by (ts, action_id) like the SQL does.
        best: dict[str, KeepRow] = {}
        for r in rows:
            b = best.get(r.prefix)
            if b is None or (r.ts, r.action_id) > (b.ts, b.action_id):
                best[r.prefix] = r
        self.best = best

    def winner(self, prefix: str) -> Optional[KeepRow]:
        """The winning row covering `prefix` (an exact ledger form,
        `gs://…/dir/`), or None if nothing covers it."""
        m = _GS_RE.match(prefix)
        if not m:
            raise ValueError(f"not a gs:// prefix: {prefix!r}")
        cands = []
        acc = f"gs://{m.group(1)}/"
        for pfx in [acc] + [acc := acc + seg + "/" for seg in m.group(2).split("/") if seg]:
            r = self.best.get(pfx)
            if r is not None:
                cands.append(r)
        if not cands:
            return None
        return max(cands, key=lambda r: (r.ts, r.action_id))

    def fate(self, prefix: str) -> Optional[str]:
        """Effective keep-state of a prefix: keep/keep_last_ckpt/sweep, or None
        (unmarked — no covering row, or the winner is an explicit unmark)."""
        w = self.winner(prefix)
        return w.keep if w else None

    def key_fate(self, bucket: str, name: str) -> tuple[Optional[str], Optional[KeepRow]]:
        """(fate, winning row) for one object key. The deepest marked ancestor
        carries the answer; we still return the *winning* row (which may sit on
        a shallower prefix) for provenance."""
        deepest = None
        for pfx in key_to_prefixes(bucket, name):
            if pfx in self.best:
                deepest = pfx
        if deepest is None:
            return None, None
        w = self.winner(deepest)
        return (w.keep if w else None), w


# ---- KLC expansion (object-level klcSplits) --------------------------------

@dataclass(frozen=True)
class KlcSplit:
    """Object-level resolution of one keep_last_ckpt band."""

    prefix: str  # the KLC mark prefix (gs:// form)
    kept: tuple[str, ...]  # kept subtree prefixes, relative to `prefix`
    resolved: bool  # False = no step-numbered level found (amber / keep+flag)


def klc_split(rel_names: Iterable[str], prefix: str = "") -> KlcSplit:
    """`rel_names` = object keys *relative to* the KLC prefix. Mirrors the tree
    walk in `site/src/sweep.ts klcSplits`: at a node whose children include
    step-numbered names, keep the max-step child (no deeper recursion) and
    sweep the rest; otherwise recurse into each child directory."""
    # Nested dict of path segments; a terminal file is a leaf ({}).
    tree: dict = {}
    for n in rel_names:
        node = tree
        for seg in n.split("/"):
            node = node.setdefault(seg, {})

    kept: list[str] = []

    def walk(node: dict, at: str) -> None:
        steps = [(seg, m) for seg in node if (m := CKPT_NUM_RE.match(seg))]
        if steps:
            best = max(steps, key=lambda s: int(s[1].group(1)))
            kept.append(f"{at}{best[0]}/")
            return
        for seg, kid in node.items():
            if kid:  # only recurse into directories (files are empty leaves)
                walk(kid, f"{at}{seg}/")

    walk(tree, "")
    return KlcSplit(prefix=prefix, kept=tuple(kept), resolved=bool(kept))


def klc_key_fate(rel_name: str, split: KlcSplit) -> str:
    """Fate of one key (relative to the KLC prefix) under a resolved split."""
    if not split.resolved:
        return "keep"  # unresolved band: conservative, flagged upstream
    u = rel_name if rel_name.endswith("/") else rel_name + "/"
    return "keep" if any(u.startswith(k) for k in split.kept) else "sweep"
