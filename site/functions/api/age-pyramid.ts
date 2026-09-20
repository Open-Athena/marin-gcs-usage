/** Multi-scale per-path created-time strata — the pyrmts pyramid backend for
 * `AgeChart` (specs/age-index.md, Phase B). Supersedes `/api/age`'s fixed day
 * bins with a bin the planner picks for the requested window + budget.
 *
 *   GET /api/age-pyramid?date=<scan>&path=<prefix>&from=<iso>&to=<iso>&bin_budget=<n>
 *
 * `planQuery` picks the finest tier (bin `1h|1d|1mo|1y`) whose bin count fits
 * `bin_budget`; we read that one complete path-major tier for `path`'s own rows
 * in `[from, to]` and return `{ records: [{ dt, b, o }], plan }` (the pyrmts
 * `usePyramid` shape — `dt` is the bin-start epoch-ms). `from`/`to` default to
 * the full range (the planner then picks the coarsest tier that fits), so the
 * FE should pass the chart's actual time domain to get useful granularity.
 */
import { type Env, requireViewer } from '../_lib/auth.js'
import { num, openIndex, readPoint } from '../_lib/index.js'
import { planAge, variantForPlan } from '../_lib/agePyramid.js'

export const onRequestGet = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  if (!ctx.env.GCS_HMAC_KEY_ID || !ctx.env.GCS_HMAC_SECRET) {
    return new Response('age-pyramid API not configured (missing GCS HMAC creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const binBudget = Math.max(1, Number(url.searchParams.get('bin_budget')) || 512)
  const fromMs = Date.parse(url.searchParams.get('from') ?? '')
  const toMs = Date.parse(url.searchParams.get('to') ?? '')
  const from = new Date(Number.isNaN(fromMs) ? 0 : fromMs)
  const to = new Date(Number.isNaN(toMs) ? Date.now() : toMs)

  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated

  const plan = planAge(from, to, binBudget)
  const variant = variantForPlan(plan)
  const meta = { outputBin: plan.outputTier?.bin ?? plan.outputBin, tier: plan.outputTier?.name ?? null, binBudget }
  const depth = path === '' ? 0 : path.split('/').length
  let rows: Record<string, unknown>[]
  try {
    const h = await openIndex(ctx.env, date, variant)
    rows = await readPoint(h, depth, path, ['path', 'depth', 'binstart', 'b', 'o'])
  } catch (e) {
    // Tier not synced (e.g. pre-backfill): empty chart, not a 500.
    if (/not synced/.test((e as Error).message)) return json({ records: [], plan: meta })
    throw e
  }
  const lo = from.getTime()
  const hi = to.getTime()
  const records = rows
    .map(r => ({ dt: num(r.binstart), b: num(r.b), o: num(r.o) }))
    .filter(r => r.dt >= lo && r.dt <= hi)
    .sort((a, b) => a.dt - b.dt)
  return json({ records, plan: meta })
}

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=86400' },
  })
