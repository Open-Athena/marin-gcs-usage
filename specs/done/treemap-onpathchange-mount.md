# `Treemap`: restore the mount-time `onPathChange` notification

Written by the `disk-tree` session (2026-08-24), after debugging the red `@disk-tree/react` job on CI run [32731212512].

[32731212512]: https://github.com/open-athena/marin-gcs-usage/actions/runs/32731212512

## Diagnosis: not environmental, and not upstream

The failing assertion is `tests/Treemap.test.tsx:62`, `calls onPathChange with the initial [root] path` → *"expected spy to be called at least once"*.

Two claims in the CI triage were wrong:

1. **"Node 20 → 24 shifted jsdom timing."** The `Node 20 is being deprecated` line refers to the runtime GitHub uses for JS *actions* (`actions/setup-node` itself). The tests run under whatever `setup-node` installs, and `.github/workflows/ci.yml` still pins `node-version: 20` in all three JS jobs. The test environment did not change.
2. **"My diff never touches `packages/react`."** `git diff <fork-base>..gcs -- packages/react/` is empty only relative to the base the CP audit used. `packages/react/src/Treemap.tsx` *is* modified on `gcs` by two fork-only commits: `34d3d04` (`initialPath`) and `f17c666` (URL-state / controlled `path`).

It reproduces 100% deterministically, locally, on Node 26:

```
$ cd ~/c/oa/marin-gcs-usage/packages/react && pnpm exec vitest run tests/Treemap.test.tsx
 × <Treemap> > calls onPathChange with the initial [root] path
   → expected "spy" to be called at least once
 Tests  1 failed | 15 passed (16)
```

Upstream `disk-tree` is unaffected — its 162 tests pass, and its CI is green.

## Root cause

The URL-state refactor replaced upstream's path-reporting effect

```ts
useEffect(() => {
  onPathChange?.(path)
}, [path, onPathChange])
```

with reporting inside the gesture handler only:

```ts
const go = useCallback((p: T[]) => {
  if (!controlled) setPathState(p)
  setTip(null)
  onPathChange?.(p)
}, [controlled, onPathChange])
```

`go` runs on drill / crumb / Backspace. Nothing reports the paths a gesture never produces:

- the **initial mount** path (what the test asserts), and
- the **`root`-change reset** — including the `initialPath` mount drill, which is the entire point of `34d3d04`. A consumer that syncs the path to the URL is told nothing when the map opens drilled into CW's lone bucket, so the URL says root while the map shows the bucket.

The live site is not currently broken by this: `site/src/App.tsx:655` uses **controlled** mode (`path={mapPath}` + `onPathChange={onMapPath}`), where the consumer already owns the path. The gap is in uncontrolled mode — which the test exercises, and which any `initialPath` consumer hits.

## Fix

Report from an effect when the component owns the path; keep `go`'s direct call for controlled mode, where no local state changes and the effect would never fire.

```ts
  const go = useCallback(
    (p: T[]) => {
      if (!controlled) setPathState(p)
      setTip(null)
      // Uncontrolled reporting happens in the effect below, so the mount and
      // root-reset paths — which no gesture produces — report too.
      if (controlled) onPathChange?.(p)
    },
    [controlled, onPathChange],
  )

  // Reset drill path when root changes (respecting `initialPath` when it
  // belongs to the new root). Skipped on mount: `useState` already seeded the
  // path, and re-seeding it here would report a second, identical `[root]`.
  const mountedRoot = useRef(root)
  useEffect(() => {
    if (controlled || mountedRoot.current === root) return
    mountedRoot.current = root
    setPathState(initialPath?.[0] === root ? initialPath : [root])
    setTip(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initialPath applies per-root, not on its own changes
  }, [root])

  // Report the path this component owns: mount (incl. `initialPath`), drills,
  // and the root-change reset.
  useEffect(() => {
    if (!controlled) onPathChange?.(path)
  }, [controlled, path, onPathChange])
```

The `mountedRoot` guard is not optional here: without it the reset effect fires on mount, hands `path` a fresh `[root]` array, and the reporting effect emits `[root]` twice before the consumer has done anything.

## Upstream companion: `54b9d4b`

`disk-tree` `main` has the same mount double-fire (its reporting effect always existed, so its reset effect produced a duplicate `[root]`). Fixed there by the same `mountedRoot` ref, in [`54b9d4b`] — cherry-pickable, though the surrounding lines differ because upstream has no controlled mode. That commit also:

- deletes `mountWithSize` from the tests. It stubbed `clientWidth`/`clientHeight` *after* mount, but the component measures synchronously in `useLayoutEffect`, so the map had zero cells and `drill-in click on a branch pushes to onPathChange` always hit its `if (cells.length === 0) return` and asserted **nothing**. That test is the natural place to catch a regression like this one, and it was inert. Your fork has the same inert test.
- rewrites that test on `withLayout()` (stubs the prototype getters *before* mount) with exact call-sequence assertions, and adds `reports the reset path exactly once when root changes`.

[`54b9d4b`]: https://github.com/runsascoded/disk-tree/commit/54b9d4b

Recommended order: take `54b9d4b`'s test rewrite (it applies to fork code nearly as-is), then apply the `go`/effect fix above — the rewritten tests then cover both modes' reporting.

## Adjacent robustness note (not fixed, not urgent)

```ts
const path = controlled && pathProp[0] === root ? pathProp : pathState
```

When a controlled `path` prop doesn't start with the current `root` — e.g. transiently across a root swap, before the consumer recomputes — the component silently renders internal `pathState` while continuing to report gestures to a consumer that thinks it is in control. The map and the URL can disagree with no path back to agreement until the prop happens to match again. Worth either asserting/warning on the mismatch, or rebasing the prop onto the new root rather than falling back.
