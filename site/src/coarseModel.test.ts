import { describe, expect, it } from 'vitest'
import { coarseError, parseCoarse, parseCoarseDiff } from './coarseModel'

const response = () => ({
  schema: 'coarse-v1', exact: true, incremental: false, date: '2026-10-04', name: 'zarr.json', mode: 'exact', path: '',
  threshold_bytes: 5, server_response_s: .2, cache_hit: true,
  tree: { path: '', label: 'all buckets', b: 12, o: 3, matches: 3, leaf: false,
    children: [{ path: 'bucket', label: 'bucket', b: 9, o: 2, matches: 2, leaf: false }],
    other: { b: 3, o: 1, matches: 1 } },
})

describe('exact coarse historical partition', () => {
  const fixture = () => {
    const before = response(), after = response()
    after.date = '2026-10-05'
    after.tree.children[0].b = 7
    after.tree.other.b = 5
    return { schema: 'coarse-diff-v1', exact: true, incremental: false, before, after,
      delta: { b: 0, o: 0, matches: 0 }, server_response_s: .4, cache_hit: true }
  }
  it('retains offsetting directory changes despite a zero parent delta', () => {
    const body = parseCoarseDiff(fixture())
    expect([body.before.tree.children?.map(c => [c.path, c.b]), body.after.tree.children?.map(c => [c.path, c.b]), body.delta])
      .toEqual([[['bucket', 9], ['', 3]], [['bucket', 7], ['', 5]], { b: 0, o: 0, matches: 0 }])
    expect([body.seconds, body.cacheHit]).toEqual([.4, true])
  })
  it('rejects independently folded child partitions', () => {
    const body = fixture()
    body.after.tree.children[0].path = 'different'
    expect(() => parseCoarseDiff(body)).toThrowError(new Error('Preview diff partitions do not align.'))
  })
  it('rejects a claimed delta inconsistent with its exact sides', () => {
    const body = fixture()
    body.delta.b = 1
    expect(() => parseCoarseDiff(body)).toThrowError(new Error('Preview diff totals are inconsistent.'))
  })
})

describe('bounded nested directory partitions', () => {
  const tree = () => {
    const body = response(), child = body.tree.children[0]
    return { ...body, schema: 'coarse-tree-v1', levels: 2, tree: { ...body.tree, children: [{ ...child,
      children: [{ path: 'bucket/file', label: 'file', b: 6, o: 1, matches: 1, leaf: true }],
      other: { b: 3, o: 1, matches: 1 },
    }] } }
  }
  it('preserves a remainder at every expanded directory', () => {
    const body = parseCoarse(tree())
    expect([body.levels, body.tree.children?.[0].children]).toEqual([2, [
      { path: 'bucket/file', label: 'file', b: 6, o: 1, matches: 1, leaf: true },
      { path: 'bucket', label: '(other)', b: 3, o: 1, matches: 1, leaf: true, folded: true },
    ]])
    expect([body.tree.b, body.other]).toEqual([12, { b: 3, o: 1, matches: 1 }])
  })
  it('checks deep partitions, not only the root', () => {
    const body = tree()
    body.tree.children[0].children[0].b = 5
    expect(() => parseCoarse(body)).toThrowError(new Error('Preview child totals do not equal their parent.'))
  })
  it('requires the same nested diff partition on both dates', () => {
    const before = tree(), after = tree()
    after.date = '2026-10-05'
    after.tree.children[0].children[0].path = 'bucket/different'
    expect(() => parseCoarseDiff({ schema: 'coarse-tree-diff-v1', exact: true, incremental: false,
      before, after, delta: { b: 0, o: 0, matches: 0 }, server_response_s: .4, cache_hit: true }))
      .toThrowError(new Error('Preview diff partitions do not align.'))
  })
  it('enforces a physical node cap even on byte-conserving trees', () => {
    const body = { ...response(), schema: 'coarse-tree-v1', levels: 2, tree: {
      path: '', label: 'all buckets', b: 2048, o: 2048, matches: 2048, leaf: false, other: { b: 0, o: 0, matches: 0 },
      children: Array.from({ length: 16 }, (_, i) => ({
        path: `dir${i}`, label: `dir${i}`, b: 128, o: 128, matches: 128, leaf: false, other: { b: 0, o: 0, matches: 0 },
        children: Array.from({ length: 128 }, (_, j) => ({ path: `dir${i}/f${j}`, label: `f${j}`, b: 1, o: 1, matches: 1, leaf: true })),
      })),
    } }
    expect(() => parseCoarse(body)).toThrowError(new Error('Preview exceeded its bounded tree size.'))
  })
})

describe('exact coarse display contract', () => {
  it('shows a bounded refusal message and handles a gateway body', () => {
    expect([coarseError(422, '{"error":"Leaf-only patterns required."}').message, coarseError(502, '<html>gateway</html>').message])
      .toEqual(['Leaf-only patterns required.', 'Coarse request failed (502).'])
  })
  it('retains exact totals and represents the remainder as a non-drillable cell', () => {
    expect(parseCoarse(response())).toEqual({
      date: '2026-10-04', name: 'zarr.json', mode: 'exact', path: '', threshold: 5, seconds: .2, cacheHit: true, levels: 1,
      tree: { path: '', label: 'all buckets', b: 12, o: 3, matches: 3, leaf: false, children: [
        { path: 'bucket', label: 'bucket', b: 9, o: 2, matches: 2, leaf: false },
        { path: '', label: '(other)', b: 3, o: 1, matches: 1, leaf: true, folded: true },
      ] }, other: { b: 3, o: 1, matches: 1 },
    })
  })
  it('rejects incomplete byte or count partitions', () => {
    const body = response()
    body.tree.children[0].b = 8
    expect(() => parseCoarse(body)).toThrowError(new Error('Preview child totals do not equal their parent.'))
  })
  it('refuses to claim exactness for integers the browser cannot represent', () => {
    const body = response()
    body.tree.b = 2 ** 54
    expect(() => parseCoarse(body)).toThrowError(new Error('Preview returned an unsupported integer total.'))
  })
})

describe('directory coverage has a distinct bytes/object contract', () => {
  const coverage = () => ({
    schema: 'coverage-v1', exact: true, incremental: false, date: '2026-10-04', pattern: 'datakit', path: '',
    threshold_bytes: 5, server_response_s: .2, cache_hit: true,
    tree: { path: '', label: 'all buckets', b: 12, o: 3, leaf: false,
      children: [{ path: 'bucket', label: 'bucket', b: 9, o: 2, leaf: false }], other: { b: 3, o: 1 } },
  })
  it('does not invent a matching-path count for covered directory rollups', () => {
    expect(parseCoarse(coverage())).toEqual({
      date: '2026-10-04', name: 'datakit', mode: 'coverage', path: '', threshold: 5, seconds: .2, cacheHit: true, levels: 1,
      tree: { path: '', label: 'all buckets', b: 12, o: 3, matches: null, leaf: false, children: [
        { path: 'bucket', label: 'bucket', b: 9, o: 2, matches: null, leaf: false },
        { path: '', label: '(other)', b: 3, o: 1, matches: null, leaf: true, folded: true },
      ] }, other: { b: 3, o: 1, matches: null },
    })
  })
  it('still verifies exact object partitions', () => {
    const body = coverage()
    body.tree.children[0].o = 1
    expect(() => parseCoarse(body)).toThrowError(new Error('Preview child totals do not equal their parent.'))
  })
  it('aligns two covered partitions without claiming a path-count delta', () => {
    const before = coverage(), after = coverage()
    after.date = '2026-10-05'
    after.tree.children[0].b = 7
    after.tree.other.b = 5
    const body = parseCoarseDiff({ schema: 'coverage-diff-v1', exact: true, incremental: false, before, after,
      delta: { b: 0, o: 0 }, server_response_s: .4, cache_hit: true })
    expect([body.before.mode, body.after.mode, body.delta]).toEqual(['coverage', 'coverage', { b: 0, o: 0, matches: null }])
  })
  it('rejects mixing leaf and directory-coverage diff contracts', () => {
    expect(() => parseCoarseDiff({ schema: 'coverage-diff-v1', exact: true, incremental: false,
      before: response(), after: coverage(), delta: { b: 0, o: 0 }, server_response_s: .4, cache_hit: true }))
      .toThrowError(new Error('Preview diff contracts do not agree.'))
  })
})
