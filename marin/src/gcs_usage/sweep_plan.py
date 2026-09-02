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


# ---- vote-model resolution (specs/vote-model.md) ---------------------------

class VoteResolver:
    """Per-user votes; sweep needs unanimity (specs/vote-model.md).

    A user's vote at a path = their most-recent covering row (the existing
    fold, partitioned by actor; NULL = retract). A path's state = the set of
    live votes: only-keep → keep, only-sweep → sweep (deletable), both →
    conflict (triage, never deleted), none → unmarked."""

    def __init__(self, rows: Iterable[KeepRow]):
        by_actor: dict[str, list[KeepRow]] = {}
        for r in rows:
            by_actor.setdefault(r.who, []).append(r)
        self.by_actor = {who: FateResolver(rs) for who, rs in by_actor.items()}

    def votes(self, prefix: str) -> dict[str, str]:
        """Live votes at `prefix`: actor → keep|keep_last_ckpt|sweep. Actors
        whose latest covering row is a retract (NULL) are absent."""
        out: dict[str, str] = {}
        for who, fr in self.by_actor.items():
            v = fr.fate(prefix)
            if v is not None:
                out[who] = v
        return out

    def state(self, prefix: str) -> str:
        """keep | sweep | conflict | unmarked."""
        return self._agg(self.votes(prefix).values())

    def key_votes(self, bucket: str, name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for who, fr in self.by_actor.items():
            v, _ = fr.key_fate(bucket, name)
            if v is not None:
                out[who] = v
        return out

    def key_state(self, bucket: str, name: str) -> str:
        return self._agg(self.key_votes(bucket, name).values())

    @staticmethod
    def _agg(votes) -> str:
        votes = list(votes)
        if not votes:
            return "unmarked"
        any_keep = any(v in ("keep", "keep_last_ckpt") for v in votes)
        any_sweep = any(v == "sweep" for v in votes)
        if any_keep and any_sweep:
            return "conflict"
        return "keep" if any_keep else "sweep"


# ---- clobbered keeps (2026-09-01 broad-sweep incident) ---------------------

@dataclass(frozen=True)
class Clobber:
    """A prefix somebody marked keep whose *effective* fate is now sweep — a
    newer covering sweep repainted it (recency beats specificity). The
    2026-09-01 case: a whole-`checkpoints/` sweep clobbering earlier keeps."""

    prefix: str
    keep: str  # keep | keep_last_ckpt (the clobbered mark)
    keeper: str
    keep_ts: int
    by_prefix: str  # the winning row that repainted it
    by_who: str
    by_ts: int
    to: str = "sweep"  # sweep | unmarked (unmarked = at risk at the deadline)


def clobbered_keeps(rows: Iterable[KeepRow]) -> list[Clobber]:
    """Every prefix with a live keep-valued row that currently resolves to
    sweep. Reports the *latest* keep-valued row per prefix as the victim."""
    rows = list(rows)
    fr = FateResolver(rows)
    latest_keep: dict[str, KeepRow] = {}
    for r in rows:
        if r.keep in ("keep", "keep_last_ckpt"):
            b = latest_keep.get(r.prefix)
            if b is None or (r.ts, r.action_id) > (b.ts, b.action_id):
                latest_keep[r.prefix] = r
    out = []
    for prefix, victim in latest_keep.items():
        w = fr.winner(prefix)
        # a winner that is the victim itself (or any keep-valued row) is fine;
        # a newer covering sweep OR unmark repainted the keep away. Unmarked
        # matters too: "unmarked is swept once the review window closes".
        if w is not None and w.keep not in ("keep", "keep_last_ckpt"):
            out.append(Clobber(
                prefix=prefix,
                keep=victim.keep,
                keeper=victim.who,
                keep_ts=victim.ts,
                by_prefix=w.prefix,
                by_who=w.who,
                by_ts=w.ts,
                to="sweep" if w.keep == "sweep" else "unmarked",
            ))
    return sorted(out, key=lambda c: (c.by_prefix, c.prefix))


def ever_kept_prefixes(rows: Iterable[KeepRow]) -> frozenset[str]:
    """Prefixes with ANY live keep-valued row, regardless of what later
    repainted them — the planner's "never delete what anyone ever marked keep"
    guard (Ryan's 2026-09-01 commitment in #internal-discuss)."""
    return frozenset(r.prefix for r in rows if r.keep in ("keep", "keep_last_ckpt"))


# ---- object-level manifest (policy (b): sweeper must own the band) ---------

#: Why a directory's keys are (or aren't) in tonight's manifest.
CATEGORIES = (
    "eligible",         # sweep-only + sweeper owns it + no keep history → delete
    "deferred_owner",   # sweep-only but the sweeper isn't the effective owner
    "deferred_unowned", # sweep-only but nobody claimed it
    "ever_kept",        # sweep-only but some ancestor once carried a keep (belt+suspenders)
    "conflict",         # keep and sweep votes both present → triage
    "klc_pending",      # keep_last_ckpt only — needs the object-level split (later phase)
    "keep",             # keep votes only
    "unmarked",         # no votes — waits for the deadline
)


def owners_resolver(actions_payload: dict) -> FateResolver:
    """Effective-owner resolution (single-value axis, actor-blind most-recent-
    wins — unchanged by the vote model). ``fate()`` returns the owner user id
    (or None = unclaimed); rides the keep-slot of :class:`KeepRow`."""
    rows = [
        KeepRow(
            prefix=r["prefix"],
            keep=r.get("owner"),
            ts=int(r["ts"]),
            action_id=int(r["action_id"]),
            who=r.get("who") or "",
        )
        for r in actions_payload["owners"]
    ]
    return FateResolver(rows)


def classify_dir(
    bucket: str,
    dirname: str,  # '' for bucket-root files, else 'a/b'
    vr: "VoteResolver",
    own: FateResolver,
    idmap,
    ever_kept: frozenset[str],
) -> tuple[str, Optional[str], tuple[str, ...]]:
    """(category, owner, sweeper ids) for one directory. Policy (b): a
    sweep-only dir is deletable tonight only when its effective owner is one
    of the sweepers (by canonical user id) and no covering prefix ever
    carried a keep."""
    prefix = f"gs://{bucket}/" + (dirname + "/" if dirname else "")
    votes = vr.votes(prefix)
    if not votes:
        return "unmarked", None, ()
    vals = set(votes.values())
    any_keep = bool(vals & {"keep", "keep_last_ckpt"})
    any_sweep = "sweep" in vals
    if any_keep and any_sweep:
        return "conflict", None, ()
    if any_keep:
        return ("klc_pending" if vals == {"keep_last_ckpt"} else "keep"), None, ()
    sweepers = tuple(sorted({idmap.resolve(w) for w in votes}))
    anc = key_to_prefixes(bucket, f"{dirname}/f" if dirname else "f")
    if any(p in ever_kept for p in anc):
        return "ever_kept", None, sweepers
    owner = own.fate(prefix)
    if owner is None:
        return "deferred_unowned", None, sweepers
    if idmap.resolve(owner) not in sweepers:
        return "deferred_owner", owner, sweepers
    return "eligible", owner, sweepers


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
