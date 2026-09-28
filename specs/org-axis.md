# Org (billing) axis — spec, tabled

**Status: spec only, deliberately tabled (2026-09-11).** Percy's ask, in the sweep-status DM thread: *"the grouping might be important so that Stanford and OA can split the costs going forward."* The old "group" axis (OA / Stanford / communal, retired 2026-09-06) grew organically on top of the first implementation and muddied the owner and mark-state axes; excising it fully was the right call. Bring it back only once several deletion round-trips with users have landed, and then as designed here, not as it was.

## What it is, and what it is not

- It answers one question: **whose bill is each byte on** — Open Athena's or Stanford's. That is a *cost* attribution, downstream of ownership.
- It is **not** a third marking axis, not a filter in the top bar, and not a pool a user chooses. Owner/assignee and mark state stay the two primary axes and the only lenses; nothing here changes what "keep", "sweep", "owned", or "unowned" mean.
- Precedence, lowest to highest emphasis: **org** < mark state < owner.

## Model

- `org` is an attribute of a **person**, in `identities.yaml`: `org: oa | stanford` (extensible). A byte's org is its owner's org; a claim (assignment) moves the byte's org with the owner, exactly as it moves everything else. No per-path org marks, ever — that was the old design's mistake.
- Bytes with **no owner** have no org. Word it distinctly from the other axes' empties: owner axis says *unowned*, mark axis says *unmarked*; the org rollup says **unbilled**. Never "unassigned", "unclaimed", "communal", "unattributed".
- Split-org paths don't exist as a concept: a directory's bytes split by owner slices already, so its org split is the same slices summed by org.

## Surfaces (least emphasis first)

1. **Totals only, at first.** `/users` gets an org rollup line above the table: `OA 812 Ti · Stanford 1,190 Ti · unbilled 96 Ti`, from the per-user rows the page already has. The estate summary on the map page gets the same line under its total. That alone answers the cost-split question.
2. **A color mode**, `org`, in the existing color dropdown (next to user / marks / tree / written / read): cells tinted by owner's org, unbilled in the neutral fill. It reuses the user color path; no new data.
3. **A per-user column** on `/users` (`org`) and on `/user/<id>` header. Editable only via `identities.yaml` (a PR), not the UI.
4. Nothing in the top bar. If a filter is ever wanted, it is `?o=org:stanford`, i.e. an *owner*-axis value (a set of owners), so it composes with mark state the way a person does and needs no new server axis.

## Data

- Server: `/api/subtree` and the estate totals already carry per-owner bytes; org is a client-side fold over `identities` (owner → org). No index change, no D1 change.
- `gcs-usage report` / the sheet sync gain an `org` column from the same map, so the monthly cost split is a pivot, not a new pipeline.

## Guardrails (the reason it was excised)

- No org appears in any decision path: `sweep manifest`, approvals, the executor, undo ignore it.
- No org value in the ledger's actions table; no `?t=`-style param; no legend pin.
- Words: `org`, `OA`, `Stanford`, `unbilled`. Not group, team, pool, communal.
