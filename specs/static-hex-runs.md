# Static name index: hash interiors are not indexed

Status: approved by Ryan 2026-10-09 (the rule); **implemented** on branch `hex-runs` (2026-10-09, "Implementation as built" below). A generic `[cloud]` pipeline option, on for both deployments. The measurements are from cw's `2026-10-09cw` (specs/cw-static-names.md).

## Why

79% of cw's suffix rows (gcs: 1%) are suffixes of names holding a run of 16+ hex digits: content-addressed `_objects/<64-hex>` stores, ray-log ids. Every interior position of a 64-hex hash is a suffix row whose `s` is a random hex string and whose `path` repeats the whole hash, so these rows are also the expensive ones (37.8 B/row on cw vs 9–10 on gcs). Nobody searches a substring from the middle of a hash. A hash's prefix (`3f9a2…`) and the whole hash stay useful, and both start at the run's first character.

Measured on three of cw's coalesced-version ranges (3.7M versions, 160.4M suffix rows): the rule without its tail keeps **27.2%** of the suffix rows (43.6M); the tail adds up to 8 rows per run (≈ +13% of a 64-hex name's 62), so expect ≈ 35–40% (not measured). The drill (match roots) shrinks with it: cw's 5,127 three-character members are mostly hex trigrams (`5c8`, `bed`, `61e`, each ~478K range rows), almost all of them matched only inside hashes.

## The rule

On the lowercase name `l` (as `NAME` computes it today), a **hex run** is a maximal substring of `[0-9a-f]` with at least `H = 16` characters. A position `p` of `l` is **opaque** iff it lies strictly inside a hex run and before its tail: `a < p ≤ b − T` for a run `l[a..b]` (0-based, inclusive; `T = 8`, the tail below). Opaque positions:

- start no suffix row (the builder skips them; every other position of ≥ 3 remaining characters is indexed as today);
- start no occurrence: a literal `q` **occurs** in a name iff `l.find(q, p) == p` for some non-opaque `p`, where a `p` in a run's tail (`b − T < p ≤ b`) counts only if the occurrence extends past `b` (`p + len(q) − 1 > b`).

"Occurs" replaces "contains" everywhere a name is matched: the first-hit test (the name holds `q`), the parent test (no ancestor segment holds `q`: apply `occurs` per segment; `q` never contains `/`), the one- and two-character literals, the catalog's census and cells, the drill's roots, brute force, and the site's path-store filter fallback, so the static and the fallback answers agree. Positions before, at and after a run are unaffected: `run.json` still matches `.json`, and a `q` starting at `a` matches the hash's prefix or the whole hash.

What changes for a user: an occurrence lying wholly inside a hash, other than at its start, no longer matches. A word glued to a hash still matches (in `…3f0data.json` the run is `…3f0da`, and `data` starts in its tail and extends past it). So only literals that are hex throughout (or with more than `T` leading hex digits) can lose matches, and those get the note below.

**Tail (required, so the note below is truthful):** also keep the last `T = 8` positions of each run (`b − T < p ≤ b`) as suffix starts. In the reader, an occurrence starting there counts only if it extends past `b`. Then every literal that contains a non-hex character and has at most `T` leading hex characters matches exactly as it does today, glued words included (`…3f0data.json` still matches `data`). Only literals that are hex throughout (`cafe`, `bad`, `1234`, a hash fragment), or that have more than `T` leading hex characters, lose matches (the ones inside long hex runs). Cost: `T` extra rows per run (64-hex: 9 of 62 positions instead of 1). Without the tail, any literal starting with `0-9a-f` (`data`, `config`) could silently lose glued matches, and the note would have to show on most searches.

## Query side: never decline, say when it matters (Ryan, 2026-10-09)

The rule is a property of the generation. `scans.json` (base) and each run's `meta.json` record `{"hex_runs": {"min": 16, "tail": 8}}`; absent = the old full index (gcs's `2026-10-08c`, cw's `2026-10-09cw`), whose readers change nothing. A deployment's tiers must agree; the runner refuses to append a run whose profile disagrees with its base's recorded rule.

- **Never decline.** Every literal is answered as usual, from the catalog, the suffix range or the drill. No 400 and no "not indexed" refusal: ordinary terms that happen to be hex (`cafe`, `bad`, `1234`, `2024`) must keep working.
- **Hex-affected** literal: it is hex throughout (`^[0-9a-f]+$` after lowercasing), or its leading hex prefix is longer than `T`. Only these can have occurrences the index skips.
- For a hex-affected literal, on a generation with `hex_runs`, the responses (`/api/name-summary`, `/api/subtree`, `/api/diff`, `/api/series`) carry `hexRuns: {min, tail}`. The filter box and `/names` then show an info icon with the note: **"Matches inside long hex IDs (16+ hex digits) aren't indexed."** No note for any other literal, or on a generation without the rule.
- The path-store fallback applies the same `occurs`, so a literal never changes answer when it moves between the static and fallback paths, and the note means the same thing on both.

## Implementation (generic, `[cloud]`)

- **Profile:** `static_profile.Profile.hex_runs: {min, tail} | None` (env `STATIC_NAMES_HEX_RUNS`, e.g. `16,8`). Required, like every field: no deployment default in code; both examples (`gcs`, `cw`) set it.
- **Builder:** one SQL macro `opaque(l, p)` (or a precomputed `list` of kept positions per name via `regexp_extract_all` over `[0-9a-f]{16,}` with positions), used by `suffix_sql`, `hist_sql`, `static_append`'s delta expansion and the catalog's short-literal pass, so every suffix row and histogram agrees. Positions are 1-based in DuckDB (`generate_series(1, length(l) - 2)`).
- **Occurs:** one Python `occurs(q, name, rule)` and one TS `occurs` (`site/functions/_lib/`), table-tested against each other on the same fixture list. `FirstHits.add`: the dedup position `at` is the first non-opaque occurrence, the parent test is `occurs` per segment. The catalog's answers walk, `static_roots`/`static_drill` (first-hit roots), `catalog brute`, `drill-brute` and the fallback filter (`view.ts` name matching) all call it.
- **Tests (exact equality, read as specs):**
  - the builder's suffix rows for a table of names equal the expected `(name, [positions])` lists: a 64-hex hash, a 15-hex run (fully indexed), a 16-hex run, two runs in one name, a run at the start and at the end, digits only (`20261009123456789` is a run), uppercase hex (lowercased first), a glued word (`…3f0data.json`), and the tail positions;
  - the reader's per-bucket answers equal a brute-force oracle using `occurs`, on every date, for hex-affected and unaffected literals (a glued `data` is found; `cafe` inside a hash is not);
  - the catalog (census, cells), the drill (roots, rollups) and an append (base + runs) equal a rebuild under the rule, byte for byte;
  - the TS reader equals the Python oracle on the shared fixture (`staticRuns.test.ts` style), the response carries `hexRuns` exactly for hex-affected literals (`cafe`, `1234` yes; `data`, `.json` no), and the fallback filter gives the same totals as the static path for the same literals;
  - a generation without `hex_runs` answers exactly as today (the existing tests, unchanged).

## Cost: rebuild cw now vs. wait

A new generation is needed either way: the rule changes `sx/`, the catalog and the drill. cw's `cintervals/` (the coalesced versions) don't change, so the rebuild starts at `suffix-map`.

| | Batch (spot) | GCS → R2 egress | Total |
|---|---:|---:|---:|
| **Rebuild cw now** as a new gen (e.g. `2026-10-10cw`): suffix-map, shards, sidecar, catalog, drill, verify; copy `sx/` + catalog + drill | ~10 VM-h ≈ $2.5–3 (this build's stages from `suffix-map` on: ≤ 26 VM-h upper bound at ~2.5–3× fewer rows) | sx ≈ 1.1–1.25B rows × ~28 B ≈ 31–35 GB; drill ≈ 40–55 GB (est.); ≈ 70–90 GB ≈ **$8.5–11** | **≈ $11–14** |
| Keep `2026-10-09cw`, copy its drill as is | 0 | drill 142.3 GB ≈ **$17** | $17 |
| Wait for the next compaction | folded into compaction | the compaction re-uploads the base anyway | ~$0 extra, but meanwhile every per-scan run and merge is ~2.5–3× bigger |

Recommendation: **rebuild cw now** (after the implementation lands and its tests pass), as a new generation beside `2026-10-09cw`. It is cheaper than copying the current drill (estimates; measure the kept fraction with the tail on a few ranges first). It gives cw heavy-term drills on R2. Every per-scan run, merge and compaction after it is ~2.5–3× smaller from the first one (specs/static-append.md, "Egress: merges re-upload"). `2026-10-09cw`'s light index stays on R2 and serves the dev stack until the new gen is verified and `STATIC_GEN` moves; deleting it is Ryan's call. gcs adopts the rule at its next compaction (1% of its rows are affected, so a rebuild just for this isn't worth it).

## Implementation as built (2026-10-09)

**One definition, three runtimes.** `cloud/src/dt_cloud/hex_runs.py`: `HexRule(min, tail)`, `dropped(l, p, m)` (an occurrence of length `m` at `p` is dropped iff `before ≥ 1 ∧ after ≥ 1 ∧ before + after ≥ min ∧ (after > tail ∨ m ≤ after)`, with `before`/`after` the hex digits just before `p` and from `p` on: the rule above in one test), `opaque`, `kept_positions`, `first_occurrence`, `occurs`, `hex_affected`, and the DuckDB forms (`kept_sql`, `first_sql`, `occurs_sql`, `grams_sql`; `rtrim`/`ltrim` over the hex digits, gated per string by `regexp_matches(l, '[0-9a-f]{16}')`, so names without a run pay nothing). `site/functions/_lib/hexRuns.ts` is the TS port. `site/functions/_lib/fixtures/hex-runs/cases.json` is the shared table (per rule `16,8` / `16,0` / off: suffix starts, first occurrences, hex-affected), checked equal to the Python rule by `cloud/tests/test_hex_runs.py` (regenerate: `HEX_RUNS_UPDATE=1`) and to the TS rule by `hexRuns.test.ts`. `/` is not hex, so a run never crosses a segment and `occurs` on a whole parent path is `occurs` per segment.

**Where the rule is recorded.** A generation's `scans.json` `hex_runs` (the build stages read it: `static_names.gen_rule` / `gen_rule_at`); every tier's `catalog/meta.json` (base: `catalog assemble`; a run: `catalog_delta`; a merged run: `merge_catalogs`) and each run's `meta.json`. The Worker reads each tier's `catalog/meta.json`; a run whose rule differs from the base's is a broken tier (the stack is cut there, logged). No `hex_runs` = the full index: `2026-10-08c` and `2026-10-09cw` answer exactly as before.

**Profile.** `Profile.hex_runs` (`STATIC_NAMES_HEX_RUNS`, `MIN,TAIL` or `off`), required: `runs add` refuses a profile without it. Both examples set `16,8`. It is the rule a **new** generation is built with (`static-names scans`, `derive`); a generation's runs always follow the generation's recorded rule. **Deviation from "the runner refuses":** a profile whose rule differs from its base's is logged, not refused, because gcs's live `2026-10-08c` (no rule) would otherwise stop taking runs until its next compaction; refusing a run is unnecessary since every stage reads the base's rule, so a run can't disagree with its base.

**Builder.** `hist_sql`, `suffix_sql` (`map_range`), `build_range`/`coalesce_range`/`coalesce_append` (`chist`), `static_append.append_open` (`dhist`), `delta_shards`, `static_catalog.expand_sql` (an append's suffix rows), `member_events` / `static_roots.member_roots` (`first_hit_sql`: the row starts at the literal's first kept occurrence and the parent doesn't hold it), `short_events` / `short_vocab_sql` / `short_roots` (`grams_sql`), `brute_sql`, `brute_view_sql`, `verify_terms`, `answer_rows`, `TieredReader`, `Drill.view` / `TieredDrill.view` (the plain-view test). Short literals (1–2 characters) are members iff they occur under the rule.

**New stages for a derived generation** (the rule changes `sx/`, the catalog and the drill, not the coalesced versions):
- `static-names derive -f FROM -g GEN`: copies FROM's `cintervals/` and `ranges.json` in the bucket, then writes GEN's `scans.json` (FROM's, with the profile's rule and `derived_from`), last; refuses an existing GEN.
- `static-names hist -g GEN`: per range, `chist/` under GEN's rule (for `plan-shards -H chist`).
- From there the build is the usual one (`suffix-map -C`, `shards`, `sidecar`, catalog, drill, verify).

**Worker / site.** `FirstHits(key, rule)`, `TierState.hexRuns`, `Drill` plain view, `indexedGate`; the path-store fallback compiles `sub` matchers with `occurs` under the deployment's static rule (`compileQuery(ast, { hexRuns })`, `hexQuery`) in subtree / diff / series / filter-cover / og, so a literal answers the same on both paths. `/api/name-summary`, `/api/subtree`, `/api/diff`, `/api/series` carry `hexRuns: {min, tail}` iff the generation has the rule and a substring of the query is hex-affected; the filter note and `/names` show an ⓘ with "Matches inside long hex IDs (16+ hex digits) aren't indexed." `RESPONSE_V` 7. Known gap: with `QUERY_BOX_URL` set, the serving box answers subtree/diff/series itself without the rule (the box isn't used by cw).

**Tests.** `cloud/tests/test_hex_runs.py` (kept positions for the spec's table, first occurrences, hex-affected, SQL = Python on random strings for four rules, the shared table); `cloud/tests/test_static_hex.py` (a fixture of hex names: suffix rows and `chist` = the kept positions; the reader, catalog census + cells + short members, member and short roots, and base ⊕ two runs (suffix rows, tiered answers and hits, catalog bytes) = brute force under `occurs` on every date, under `16,8` and without a rule; the literals whose answer the rule changes are exactly `cafe 1234 0123 bcdef 2b3c 7c1d e8f9 6789 89abcdef0y c3d4`); `test_static_runner.py` (the profile field, the mismatch log); site `hexRuns.test.ts`, `staticHex.test.ts`, `staticRuns.test.ts`, `filterNote.test.ts`. Mutation checks: the tail treated as opaque in SQL (4 fail), `first_sql` ignoring the rule (2 fail), the reader's parent test as plain `in` (1 fails); TS: `dropped` ignoring the tail (6 fail), `FirstHits`' parent test as `includes` (1 fails).
