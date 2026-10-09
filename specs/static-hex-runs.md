# Static name index: hash interiors are not indexed

Status: approved by Ryan 2026-10-09 (the rule), not built. A generic `[cloud]` pipeline option, on for both deployments. Written for the agent that implements it; the measurements are from cw's `2026-10-09cw` (specs/cw-static-names.md).

## Why

79% of cw's suffix rows (gcs: 1%) are suffixes of names holding a run of 16+ hex digits: content-addressed `_objects/<64-hex>` stores, ray-log ids. Every interior position of a 64-hex hash is a suffix row whose `s` is a random hex string and whose `path` repeats the whole hash, so these rows are also the expensive ones (37.8 B/row on cw vs 9–10 on gcs). Nobody searches a substring from the middle of a hash. A hash's prefix (`3f9a2…`) and the whole hash stay useful, and both start at the run's first character.

Measured on three of cw's coalesced-version ranges (3.7M versions, 160.4M suffix rows): the rule below keeps **27.2%** of the suffix rows (43.6M). The drill (match roots) shrinks with it: cw's 5,127 three-character members are mostly hex trigrams (`5c8`, `bed`, `61e`, each ~478K range rows), almost all of them matched only inside hashes.

## The rule

On the lowercase name `l` (as `NAME` computes it today), a **hex run** is a maximal substring of `[0-9a-f]` with at least `H = 16` characters. A position `p` of `l` is **opaque** iff it lies strictly inside a hex run: `a < p ≤ b` for a run `l[a..b]` (0-based, inclusive). Opaque positions:

- start no suffix row (the builder skips them; every other position of ≥ 3 remaining characters is indexed as today);
- start no occurrence: a literal `q` **occurs** in a name iff `l.find(q, p) == p` for some non-opaque `p`.

"Occurs" replaces "contains" everywhere a name is matched: the first-hit test (the name holds `q`), the parent test (no ancestor segment holds `q`: apply `occurs` per segment; `q` never contains `/`), the one- and two-character literals, the catalog's census and cells, the drill's roots, brute force, and the site's path-store filter fallback, so the static and the fallback answers agree. Positions before, at and after a run are unaffected: `run.json` still matches `.json`, and a `q` starting at `a` matches the hash's prefix or the whole hash.

What changes for a user: an occurrence that starts strictly inside a hash no longer matches. That includes a word glued to a hash with no separator whose first character is hex: in `…3f0data.json` the run is `…3f0da`, so `data` starts inside it. Hashes in these stores are delimited (`_`, `-`, `.`, `/`) in every sample looked at, so this is rare.

**Optional refinement (recommended, decide at implementation):** also keep the last `T = 8` positions of each run (`b − T < p ≤ b`) as suffix starts. In the reader, an occurrence starting there counts only if it extends past `b`. Then every literal that contains a non-hex character and has at most `T` leading hex characters matches exactly as it does today, glued words included. Only literals that are hex throughout (`2024`, `bed`, `cafe`, a hash fragment), or that have more than `T` leading hex characters, see the new semantics. Cost: `T` extra rows per run (64-hex: 9 of 62 positions instead of 1).

## Query side: never a silent zero

The rule is a property of the generation. `scans.json` (base) and each run's `meta.json` record `{"hex_runs": {"min": 16, "tail": 0|8}}`; absent = the old full index (gcs's `2026-10-08c`, cw's `2026-10-09cw`), whose readers change nothing. A deployment's tiers must agree; the runner refuses to append a run whose profile disagrees with its base's recorded rule.

A literal is **hex-affected** iff it could occur at an opaque position: its first character is hex (with the tail refinement: it is hex throughout, or its leading hex prefix is longer than `T`). For a hex-affected literal the answer is exact under "occurs", and the responses say so:

- `/api/name-summary` and the map filter (`/api/subtree`, `/api/diff`, `/api/series`) return `hexRuns: {min, tail}` beside the answer.
- The filter box and `/names` show one line: "Matches inside long hex ids (16+ hex digits) aren't counted, except from the id's start".
- The path-store fallback applies the same `occurs`, so a literal never changes answer when it moves between the static and fallback paths.

Declining (a 400 `hex-interior`) instead was considered and rejected. Without the tail refinement, every literal starting with `0-9a-f` is hex-affected (`data`, `config`, `checkpoint`, `eval`, `2024`, …), so declining would refuse most searches. With the refinement, decline is viable for literals that are hex throughout; offer it as a deployment flag only if Ryan prefers refusing `2024` to answering it under the new semantics.

## Implementation (generic, `[cloud]`)

- **Profile:** `static_profile.Profile.hex_runs: {min, tail} | None` (env `STATIC_NAMES_HEX_RUNS`, e.g. `16` or `16,8`). Required, like every field: no deployment default in code; both examples (`gcs`, `cw`) set it.
- **Builder:** one SQL macro `opaque(l, p)` (or a precomputed `list` of kept positions per name via `regexp_extract_all` over `[0-9a-f]{16,}` with positions), used by `suffix_sql`, `hist_sql`, `static_append`'s delta expansion and the catalog's short-literal pass, so every suffix row and histogram agrees. Positions are 1-based in DuckDB (`generate_series(1, length(l) - 2)`).
- **Occurs:** one Python `occurs(q, name, rule)` and one TS `occurs` (`site/functions/_lib/`), table-tested against each other on the same fixture list. `FirstHits.add`: the dedup position `at` is the first non-opaque occurrence, the parent test is `occurs` per segment. The catalog's answers walk, `static_roots`/`static_drill` (first-hit roots), `catalog brute`, `drill-brute` and the fallback filter (`view.ts` name matching) all call it.
- **Tests (exact equality, read as specs):**
  - the builder's suffix rows for a table of names equal the expected `(name, [positions])` lists: a 64-hex hash, a 15-hex run (fully indexed), a 16-hex run, two runs in one name, a run at the start and at the end, digits only (`20261009123456789` is a run), uppercase hex (lowercased first), a glued word (`…3f0data.json`), and, with `tail = 8`, the tail positions;
  - the reader's per-bucket answers equal a brute-force oracle using `occurs`, on every date, for hex-affected and unaffected literals, with and without the tail;
  - the catalog (census, cells), the drill (roots, rollups) and an append (base + runs) equal a rebuild under the rule, byte for byte;
  - the TS reader equals the Python oracle on the shared fixture (`staticRuns.test.ts` style), the response carries `hexRuns`, and the fallback filter gives the same totals as the static path for the same literals;
  - a generation without `hex_runs` answers exactly as today (the existing tests, unchanged).

## Cost: rebuild cw now vs. wait

A new generation is needed either way: the rule changes `sx/`, the catalog and the drill. cw's `cintervals/` (the coalesced versions) don't change, so the rebuild starts at `suffix-map`.

| | Batch (spot) | GCS → R2 egress | Total |
|---|---:|---:|---:|
| **Rebuild cw now** as a new gen (e.g. `2026-10-10cw`): suffix-map, shards, sidecar, catalog, drill, verify; copy `sx/` + catalog + drill | ~10 VM-h ≈ $2.5–3 (this build's stages from `suffix-map` on: ≤ 26 VM-h upper bound at 3.7× the rows) | sx ≈ 0.85B rows × ~28 B ≈ 24 GB; drill ≈ 30–45 GB (est.); ≈ 55–70 GB ≈ **$7–8.5** | **≈ $10–11** |
| Keep `2026-10-09cw`, copy its drill as is | 0 | drill 142.3 GB ≈ **$17** | $17 |
| Wait for the next compaction | folded into compaction | the compaction re-uploads the base anyway | ~$0 extra, but meanwhile every per-scan run and merge is ~3.7× bigger |

Recommendation: **rebuild cw now** (after the implementation lands and its tests pass), as a new generation beside `2026-10-09cw`. It is cheaper than copying the current drill. It gives cw heavy-term drills on R2. Every per-scan run, merge and compaction after it is ~3.7× smaller from the first one (specs/static-append.md, "Egress: merges re-upload"). `2026-10-09cw`'s light index stays on R2 and serves the dev stack until the new gen is verified and `STATIC_GEN` moves; deleting it is Ryan's call. gcs adopts the rule at its next compaction (1% of its rows are affected, so a rebuild just for this isn't worth it).
