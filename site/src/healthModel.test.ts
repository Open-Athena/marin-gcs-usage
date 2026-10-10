// specs/health-page.md: the /health computation over a fixture of both stores. The interval store missed scan 04 (an
// out-of-order append can never join) and has two level-0 runs due to carry; the static index's run for 05 has no
// drill, which cuts the drill stack there (06's drill is built but unserved), and the two level-0 runs never carry
// (one drilled, one not). Scan 07's job is still running.
import { describe, expect, it } from 'vitest'
import {
  compaction, coverage, coverageTiers, freshness, type HealthRun, healthDoc, planCarries, stackTiers, type StackRun,
  storeHealth, type StoreRead,
} from './healthModel'

const D = (d: string) => `2026-10-${d}`
const run = (first: string, last: string, level: number, scans: string[], extra: Partial<HealthRun> = {}): HealthRun => ({
  key: first === last ? `deltas/${D(first)}` : `deltas/${D(first)}_${D(last)}`, first: D(first), last: D(last), level, scans: scans.map(D), r2: true, ...extra,
})
const all = { light: true, drill: true, anchors: true }

const IV: StoreRead = {
  kind: 'interval', gen: 'iv1', manifest: '2026-10-06.json', rev: 0, revises: null, manifest_ts: 1_791_000_000, compact_level: 5,
  base: ['01', '02', '03'].map(D),
  runs: [run('05', '05', 0, ['05']), run('06', '06', 0, ['06'])],
}
const ST: StoreRead = {
  kind: 'static', gen: 'sn1', manifest: '2026-10-06.m001.json', rev: 1, revises: 'manifests/2026-10-06.json', manifest_ts: 1_791_100_000, compact_level: 5,
  base: ['01', '02'].map(D), base_tiers: all,
  runs: [
    run('03', '04', 1, ['03', '04'], { tiers: all }),
    run('05', '05', 0, ['05'], { tiers: { ...all, drill: false } }),
    run('06', '06', 0, ['06'], { tiers: all }),
  ],
}
const NOW = Date.UTC(2026, 9, 8) / 1000
const PER_SCAN = ['01', '02', '03', '04', '05'].map(D)
const JOBS = [{ scan: D('06'), status: 'succeeded' }, { scan: D('07'), status: 'running' }]

describe('planCarries (append_runner.plan_carries)', () => {
  const r = (k: string, level: number, n = 1): StackRun => ({ key: `deltas/${k}`, first: k, last: k, level, scans: Array.from({ length: n }, (_, i) => `${k}.${i}`) })
  it('replays the binary counter: chained carries fold into one N-way merge', () => {
    const { after, carries } = planCarries([r('a', 1, 2), r('b', 0), r('c', 0)])
    expect(after.map(x => [x.key, x.level, x.scans.length])).toEqual([['deltas/a_c', 2, 4]])
    expect(carries).toEqual([{ inputs: ['deltas/a', 'deltas/b', 'deltas/c'], output: 'deltas/a_c', level: 2, first: 'a', last: 'c', scans: 4 }])
  })
  it('never mixes a drilled run with an undrilled one, nor carries into the compaction level', () => {
    expect(planCarries([r('a', 0), r('b', 0)], new Set(['deltas/a'])).carries).toEqual([])
    expect(planCarries([r('a', 4, 16), r('b', 4, 16)]).carries).toEqual([])
    expect(compaction(planCarries([r('a', 4, 16), r('b', 4, 16)]).after, 5)).toEqual({ level: 5, run_scans: 32, capacity: 32, due: true, max_level: 4 })
  })
})

describe('storeHealth', () => {
  it('interval: a due carry, the gauge, every scan served', () => {
    const h = storeHealth(IV)
    expect([h.carries, h.compaction, h.newest, h.served, h.cut]).toEqual([
      [{ inputs: ['deltas/2026-10-05', 'deltas/2026-10-06'], output: 'deltas/2026-10-05_2026-10-06', level: 1, first: D('05'), last: D('06'), scans: 2 }],
      { level: 5, run_scans: 2, capacity: 32, due: false, max_level: 1 },
      D('06'),
      { light: ['01', '02', '03', '05', '06'].map(D), drill: null, anchors: null },
      { light: null, drill: null, anchors: null },
    ])
  })
  it('static: the drill stack is cut at the drill-less run; no carry across it', () => {
    const h = storeHealth(ST)
    expect([h.carries, h.compaction, h.served, h.cut]).toEqual([
      [],
      { level: 5, run_scans: 4, capacity: 32, due: false, max_level: 1 },
      { light: ['01', '02', '03', '04', '05', '06'].map(D), drill: ['01', '02', '03', '04'].map(D), anchors: ['01', '02', '03', '04', '05', '06'].map(D) },
      { light: null, drill: 'deltas/2026-10-05', anchors: null },
    ])
  })
})

describe('coverage', () => {
  const scans = ['01', '02', '03', '04', '05', '06', '07'].map(D)
  const cov = (st: StoreRead = ST) => coverage(scans, new Set(PER_SCAN), new Set([D('07')]), [storeHealth(IV), storeHealth(st)])
  const grid = (c: ReturnType<typeof cov>) => c.map(x => [x.scan.slice(8), x.cells.path, x.cells.interval, x.cells.light, x.cells.drill, x.cells.anchors, x.cells.r2, x.gaps.join(',')])
  it('flags the scan missing from the interval store and the drill gaps; the running scan is pending', () => {
    expect(grid(cov())).toEqual([
      ['01', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['02', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['03', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['04', 'present', 'missing', 'present', 'present', 'present', 'present', 'interval'],
      ['05', 'present', 'present', 'present', 'missing', 'present', 'present', 'drill'],
      ['06', 'present', 'present', 'present', 'missing', 'present', 'present', 'drill'],
      ['07', 'pending', 'pending', 'pending', 'pending', 'pending', 'na', ''],
    ])
  })
  it('a listed run not whole on R2 cuts the light stack there: its scan and every later one', () => {
    const broken = { ...ST, runs: ST.runs.map(r => (r.key === 'deltas/2026-10-05' ? { ...r, r2: false } : r)) }
    expect(grid(cov(broken)).slice(4, 6)).toEqual([
      ['05', 'present', 'present', 'missing', 'missing', 'missing', 'missing', 'light,drill,anchors,r2'],
      ['06', 'present', 'present', 'missing', 'missing', 'missing', 'present', 'light,drill,anchors'],
    ])
  })
  it('a store that isn\'t configured is `na` in its columns', () => {
    const c = coverage([D('01')], new Set([D('01')]), new Set(), [storeHealth(IV)])
    expect(grid(c)).toEqual([['01', 'present', 'present', 'na', 'na', 'na', 'present', '']])
  })
})

describe('freshness and the document', () => {
  it('ages from the scan ids and the manifests\' uploads', () => {
    const doc = healthDoc([IV, ST], PER_SCAN, JOBS, NOW)
    expect([doc.scans, doc.gaps, doc.freshness]).toEqual([
      ['01', '02', '03', '04', '05', '06', '07'].map(D),
      { path: 0, interval: 1, light: 0, drill: 2, anchors: 0, r2: 0 },
      {
        newest_scan: D('07'), newest_scan_age: 86_400,
        stores: [
          { kind: 'interval', newest: D('06'), newest_age: 172_800, manifest_ts: 1_791_000_000, manifest_age: NOW - 1_791_000_000 },
          { kind: 'static', newest: D('06'), newest_age: 172_800, manifest_ts: 1_791_100_000, manifest_age: NOW - 1_791_100_000 },
        ],
      },
    ])
    expect(freshness([], [], NOW)).toEqual({ newest_scan: null, newest_scan_age: null, stores: [] })
  })
})

describe('a running job whose rows have `started_ts` NULL (gcs, 2026-10-10)', () => {
  // T1514's job has published its per-scan path index; its interval and static appends haven't landed. Neither 10-10
  // run recorded a start (the job bug fixed in 6842ed63).
  const T = (iso: string) => Date.parse(iso) / 1000
  const X = (id: string) => `2026-10-${id}`
  const iv: StoreRead = { ...IV, manifest_ts: T('2026-10-10T03:00:00Z'), base: ['09T1236'].map(X), runs: [{ key: 'deltas/2026-10-10', first: X('10'), last: X('10'), level: 0, scans: [X('10')], r2: true }] }
  const st: StoreRead = { ...ST, manifest_ts: T('2026-10-10T03:10:00Z'), base: ['09T1236', '10'].map(X), runs: [] }
  const per = ['09T1236', '10', '10T1514'].map(X)
  const NOW10 = T('2026-10-10T16:00:00Z')
  const jobs = (status1514: string) => [
    { scan: X('09T1236'), status: 'succeeded', started_ts: T('2026-10-09T12:36:30Z') },
    { scan: X('10'), status: 'succeeded', started_ts: null },
    { scan: X('10T1514'), status: status1514, started_ts: null },
  ]
  const grid = (d: ReturnType<typeof healthDoc>) => d.coverage.map(x => [x.scan, x.cells.path, x.cells.interval, x.cells.light, x.cells.drill, x.cells.anchors, x.cells.r2])
  it('the running scan is pending in the stores it hasn\'t reached, not a gap', () => {
    const doc = healthDoc([iv, st], per, jobs('running'), NOW10)
    expect([grid(doc), doc.gaps]).toEqual([
      [
        [X('09T1236'), 'present', 'present', 'present', 'present', 'present', 'present'],
        [X('10'), 'present', 'present', 'present', 'present', 'present', 'present'],
        [X('10T1514'), 'present', 'pending', 'pending', 'pending', 'pending', 'na'],
      ],
      { path: 0, interval: 0, light: 0, drill: 0, anchors: 0, r2: 0 },
    ])
  })
  it('mutation: once its job has stopped, the same scan\'s absences are gaps', () => {
    const doc = healthDoc([iv, st], per, jobs('failed'), NOW10)
    expect([grid(doc).slice(2), doc.gaps]).toEqual([
      [[X('10T1514'), 'present', 'missing', 'missing', 'missing', 'missing', 'na']],
      { path: 0, interval: 1, light: 1, drill: 1, anchors: 1, r2: 0 },
    ])
  })
  it('a finished job\'s scan that nothing serves is `unserved`, not a coverage row with gaps', () => {
    const doc = healthDoc([iv, st], per, [...jobs('running'), { scan: '2026-08-10', status: 'succeeded', started_ts: T('2026-08-10T07:01:25Z') }], NOW10)
    expect([doc.scans, doc.unserved, doc.gaps]).toEqual([
      [X('09T1236'), X('10'), X('10T1514')],
      ['2026-08-10'],
      { path: 0, interval: 0, light: 0, drill: 0, anchors: 0, r2: 0 },
    ])
  })
  it('a running job that names no scan (its id unmatched) still makes a scan past every store\'s newest pending — only that one', () => {
    const scans = ['09T1236', '10', '10T1514'].map(X)
    const stores = [{ ...iv, base: [X('09T1236')], runs: [] }, st].map(storeHealth)
    const c = coverage(scans, new Set(), new Set(), stores, true)
    expect(c.map(x => [x.scan, x.cells.path, x.cells.interval, x.cells.light])).toEqual([
      [X('09T1236'), 'present', 'present', 'present'],
      [X('10'), 'missing', 'missing', 'present'],
      [X('10T1514'), 'pending', 'pending', 'pending'],
    ])
    expect(coverage(scans, new Set(), new Set(), stores, false).map(x => x.cells.interval)).toEqual(['present', 'missing', 'missing'])
  })
  it('ages a scan from its job\'s start when known, else its id\'s time', () => {
    const withStart = [...jobs('running').slice(0, 1), { scan: X('10'), status: 'succeeded', started_ts: T('2026-10-10T01:00:00Z') }, ...jobs('running').slice(2)]
    expect(healthDoc([iv, st], per, withStart, NOW10).freshness).toEqual({
      newest_scan: X('10T1514'), newest_scan_age: T('2026-10-10T16:00:00Z') - T('2026-10-10T15:14:00Z'),
      stores: [
        { kind: 'interval', newest: X('10'), newest_age: 15 * 3600, manifest_ts: T('2026-10-10T03:00:00Z'), manifest_age: 13 * 3600 },
        { kind: 'static', newest: X('10'), newest_age: 15 * 3600, manifest_ts: T('2026-10-10T03:10:00Z'), manifest_age: 13 * 3600 - 600 },
      ],
    })
    expect(healthDoc([iv, st], per, jobs('running'), NOW10).freshness.stores.map(f => f.newest_age)).toEqual([16 * 3600, 16 * 3600])
  })
})

describe('stack → shards (CoverTimeline rows)', () => {
  const axis = ['01', '02', '03', '04', '05', '06', '07'].map(D)
  const t = (d: string) => `${D(d)}T00:00:00.000Z`
  const shape = (rows: ReturnType<typeof stackTiers>) => rows.map(r => [r.tier, r.segments.map(s => [s.start.slice(8, 10), s.end.slice(8, 10), s.status, s.shardDur, s.key])])
  it('interval: the base, a row per level down from the compaction\'s, the due carry pending on its level', () => {
    const rows = stackTiers(storeHealth(IV), axis, NOW * 1000)
    expect(shape(rows)).toEqual([
      ['base', [['01', '04', 'present', '3 scans', 'iv1 (base)']]],
      ['L4', []], ['L3', []], ['L2', []],
      ['L1', [['05', '07', 'pending', 'due carry of 2 runs · 2 scans', 'deltas/2026-10-05_2026-10-06']]],
      ['L0', [['05', '06', 'present', '1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', '1 scan', 'deltas/2026-10-06']]],
    ])
    expect(rows[0].segments[0]).toEqual({ start: t('01'), end: t('04'), shardDur: '3 scans', status: 'present', key: 'iv1 (base)' })
    expect(rows.map(r => [r.totalExpected, r.totalPresent, r.totalPending, r.complete])).toEqual([[1, 1, 0, true], [0, 0, 0, true], [0, 0, 0, true], [0, 0, 0, true], [1, 0, 1, true], [2, 2, 0, true]])
  })
  it('static: drill and anchors rows mark the drill-less run missing', () => {
    expect(shape(stackTiers(storeHealth(ST), axis, NOW * 1000)).slice(4)).toEqual([
      ['L1', [['03', '05', 'present', '2 scans', 'deltas/2026-10-03_2026-10-04']]],
      ['L0', [['05', '06', 'present', '1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', '1 scan', 'deltas/2026-10-06']]],
      ['drill', [['01', '03', 'present', 'drill · base', 'sn1 (base)'], ['03', '05', 'present', 'drill · 2 scans', 'deltas/2026-10-03_2026-10-04'], ['05', '06', 'missing', 'drill · 1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', 'drill · 1 scan', 'deltas/2026-10-06']]],
      ['anch', [['01', '03', 'present', 'anchors · base', 'sn1 (base)'], ['03', '05', 'present', 'anchors · 2 scans', 'deltas/2026-10-03_2026-10-04'], ['05', '06', 'present', 'anchors · 1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', 'anchors · 1 scan', 'deltas/2026-10-06']]],
    ])
  })
  it('coverage rows: a shard per scan, `na` left out', () => {
    const doc = healthDoc([IV], PER_SCAN, JOBS, NOW)
    expect(coverageTiers(doc.coverage, ['interval'], NOW * 1000).map(r => [r.tier, r.segments.map(s => [s.shardDur.slice(8), s.status])])).toEqual([
      ['iv', [['01', 'present'], ['02', 'present'], ['03', 'present'], ['04', 'missing'], ['05', 'present'], ['06', 'present'], ['07', 'pending']]],
    ])
  })
})
