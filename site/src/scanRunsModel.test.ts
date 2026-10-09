// specs/scan-runs-ui.md: the derivations /scans shows from the D1 rows.
import { describe, expect, it } from 'vitest'
import {
  batchHref, failedAt, logsHref, mapHref, metaHref, outputIndex, phaseOrder, phaseSpans, type ScanRun, type ScanRunOutput,
  type ScanRunPhase, structureOf, summarize, withDeltas,
} from './scanRunsModel'

export const run = (o: Partial<ScanRun> & Pick<ScanRun, 'run_id' | 'scan'>): ScanRun => ({
  store: 'primary', kind: 'scan', parent: null, status: 'succeeded', failed_phase: null, error: null, started_ts: 1000,
  finished_ts: 5000, job_name: null, region: null, machine: null, spot: null, tasks: null, image: null, cost_usd: null,
  gen: null, source: 'backfill', updated_ts: 0, ...o,
})
export const out = (run_id: string, key: string, bytes: number | null, rows: number | null = null): ScanRunOutput =>
  ({ run_id, key, uri: `gs://d/${key}/`, bytes, objects: 1, rows, gen: null })
export const phase = (run_id: string, name: string, seq: number, started_ts: number, finished_ts: number): ScanRunPhase =>
  ({ run_id, phase: name, seq, started_ts, finished_ts, status: 'done', note: null })

const RUNS = [
  run({ run_id: 'a', scan: '2026-10-06', started_ts: 100, finished_ts: 400, cost_usd: 1 }),
  run({ run_id: 'b-fail', scan: '2026-10-07', status: 'failed', started_ts: 1000, finished_ts: 1100 }),
  run({ run_id: 'b', scan: '2026-10-07', started_ts: 2000, finished_ts: 2600, cost_usd: 2 }),
  run({ run_id: 'b-list', scan: '2026-10-07', kind: 'listing', parent: 'b', started_ts: 2010, finished_ts: 2300, cost_usd: 0.5 }),
  run({ run_id: 'c', scan: '2026-10-08', status: 'running', started_ts: 3000, finished_ts: null }),
]
const OUTS = [
  out('a', 'listing', 100, 10), out('a', 'listing/b1', 100, 10), out('a', 'static', 5), out('a', 'static/deltas/2026-10-06', 5),
  out('b-fail', 'listing', 999),
  out('b', 'listing', 130, 12), out('b', 'listing/b1', 130, 12), out('b', 'snapshot', 7), out('b', 'static', 8), out('b', 'static/deltas/2026-10-07', 8),
  out('c', 'listing', 120, null),
]
const PHASES = [
  phase('b', 'listing', 0, 2000, 2300), phase('b', 'ingest', 1, 2000, 2310), phase('b', 'index', 2, 2310, 2500),
  phase('b-fail', 'listing', 0, 1000, 1050),
]

describe('deltas against the previous scan', () => {
  it('compares each structure to the newest earlier scan that has it; a failed run is no baseline', () => {
    const idx = outputIndex(RUNS, OUTS)
    expect(withDeltas(RUNS[2], OUTS, idx).map(o => [o.key, o.prev_scan, o.delta_bytes, o.delta_rows])).toEqual([
      ['listing', '2026-10-06', 30, 2],
      ['listing/b1', '2026-10-06', 30, 2],
      ['snapshot', null, null, null],
      ['static', '2026-10-06', 3, null],
      ['static/deltas/2026-10-07', '2026-10-06', 3, null],
    ])
    expect(withDeltas(RUNS[4], OUTS, idx).map(o => [o.key, o.prev_scan, o.delta_bytes, o.delta_rows])).toEqual([
      ['listing', '2026-10-07', -10, null],
    ])
  })
  it('abstracts the scan id out of a key', () => {
    expect([structureOf('static/deltas/2026-10-07', '2026-10-07'), structureOf('listing/b1', '2026-10-07')]).toEqual(['static/deltas/{scan}', 'listing/b1'])
  })
})

describe('summarize: one row per orchestrating run, newest first', () => {
  it('folds downstream jobs in, totals top-level outputs, names failures', () => {
    expect(summarize(RUNS, PHASES, OUTS, 3500).map(s => [s.run.run_id, s.secs, s.out_bytes, s.delta_bytes, s.downstream, s.cost_usd, s.failed_at, s.spans.length])).toEqual([
      ['c', 500, 120, -10, 0, null, null, 0],
      ['b', 600, 145, 33, 1, 2.5, null, 3],
      ['b-fail', 100, 999, 899, 0, null, 'after listing', 1],
      ['a', 300, 105, null, 0, 1, null, 0],
    ])
  })
})

describe('phase spans', () => {
  it('puts an overlapped phase on its own lane, on the run clock', () => {
    expect(phaseSpans(RUNS[2], PHASES.filter(p => p.run_id === 'b')).map(s => [s.phase, s.start, s.end, s.lane])).toEqual([
      ['listing', 0, 300, 0], ['ingest', 0, 310, 1], ['index', 310, 500, 0],
    ])
  })
  it('orders phase names by their mean position', () => {
    expect(phaseOrder([...PHASES, phase('x', 'index', 1, 0, 1)])).toEqual(['listing', 'ingest', 'index'])
  })
  it('a recorded failed phase wins over "after"', () => {
    expect([failedAt({ ...RUNS[1], failed_phase: 'index' }, PHASES), failedAt(RUNS[2], PHASES)]).toEqual(['index', null])
  })
})

describe('links', () => {
  it('meta tree: a prefix opens itself, an object or split part its directory; no base, no link', () => {
    expect([
      metaHref('https://h.test/meta', 'gs://data/listing/2026-10-08/'),
      metaHref('https://h.test/meta/', 'gs://data/listing/2026-10-08/index/g/path-index'),
      metaHref('https://h.test/meta', 'gs://data/top.json'),
      metaHref(null, 'gs://data/listing/'),
      metaHref('https://h.test/meta', 'not a uri'),
    ]).toEqual([
      'https://h.test/meta/data/listing/2026-10-08',
      'https://h.test/meta/data/listing/2026-10-08/index/g',
      'https://h.test/meta/data',
      null,
      null,
    ])
  })
  it('map, Batch and logs', () => {
    expect([mapHref('2026-10-08'), mapHref('2026-10-09T1236'), mapHref('nope')]).toEqual(['/?d=261008', '/?d=2610091236', null])
    expect(batchHref('p', 'us-central1', 'job-1')).toBe('https://console.cloud.google.com/batch/jobsDetail/regions/us-central1/jobs/job-1/details?project=p')
    expect(decodeURIComponent(logsHref('p', { run_id: 'uid-1', started_ts: 1_800_000_000 }))).toBe(
      'https://console.cloud.google.com/logs/query;query=labels.job_uid="uid-1"\nlog_id("batch_task_logs")\ntimestamp >= "2027-01-15T07:50:00.000Z"?project=p')
  })
})
